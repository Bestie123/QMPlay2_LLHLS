#!/usr/bin/env python3
"""
QMPlay2 Portable Deploy Script

Создаёт portable-сборку QMPlay2 со всеми зависимостями из результата сборки
(ninja). Раскладка после сборки:
  build/src/gui/QMPlay2.exe        - исполняемый файл
  build/src/gui/qt.conf            - конфигурация Qt
  build/src/qmplay2/libqmplay2.dll - ядро
  build/src/modules/*/*.dll        - модули (Extensions, FFmpeg, ...)
  build/lang/*.qm                  - переводы

Скрипт:
  1. Копирует exe, libqmplay2.dll, qt.conf
  2. Копирует модули и переводы
  3. Запускает windeployqt (Qt DLL + плагины: platforms, imageformats, ...)
  4. Через objdump рекурсивно находит и копирует системные/MinGW DLL
     (libgcc, libstdc++, libwinpthread, zlib, ...)
  5. Проверяет наличие ключевых файлов и упаковывает zip

Использование:
    python scripts/deploy.py [--mingw-bin PATH] [--output PATH] [--skip-build] [--zip PATH]
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def run(cmd, cwd=None, check=True):
    """Запустить команду и вернуть результат."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}")
        print(f"STDERR: {result.stderr[:500]}")
        sys.exit(1)
    return result


def get_dll_deps(dll_path):
    """Получить список DLL-зависимостей через objdump."""
    try:
        result = subprocess.run(
            ["objdump", "-p", str(dll_path)],
            capture_output=True, text=True, timeout=30
        )
        deps = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("DLL Name:"):
                deps.append(stripped.split("DLL Name:")[1].strip())
        return deps
    except Exception:
        return []


def find_dll(name, search_dirs):
    """Найти DLL в поисковых директориях."""
    name_lower = name.lower()
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and f.name.lower() == name_lower:
                return f
    return None


def collect_system_dlls(output_dir, mingw_bin):
    """Рекурсивно найти и скопировать системные/MinGW DLL-зависимости."""
    search_dirs = [mingw_bin, output_dir]
    # Сначала копируем обязательные рантайм-библиотеки MinGW, если они есть
    required = ["libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"]
    for r in required:
        src = find_dll(r, [mingw_bin])
        if src and not (output_dir / r).exists():
            shutil.copy2(src, output_dir / r)

    seen = set()
    changed = True
    # Итеративно: пока появляются новые DLL, проверяем их зависимости
    while changed:
        changed = False
        all_dlls = list(output_dir.glob("*.dll"))
        for dll in all_dlls:
            if dll.name.lower() in seen:
                continue
            seen.add(dll.name.lower())
            for dep in get_dll_deps(dll):
                dep_lower = dep.lower()
                if (output_dir / dep).exists():
                    continue
                src = find_dll(dep, search_dirs)
                if src:
                    shutil.copy2(src, output_dir / dep)
                    changed = True


def collect_runtime_dlls(output_dir, mingw_bin):
    """Самодиагностика: запустить QMPlay2 с полным PATH, перечислить загруженные
    модули и скопировать те, что берутся из mingw64/bin (напр. libtag-2.dll),
    но отсутствуют в портативной папке. Без них exe не запускается на чистой
    машине (ошибка 0xC0000135 - DLL not found)."""
    import time
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    exe = (output_dir / "QMPlay2.exe").resolve()
    if not exe.exists():
        return

    env = os.environ.copy()
    env["PATH"] = str(mingw_bin) + os.pathsep + env.get("PATH", "")

    try:
        proc = subprocess.Popen([str(exe)], env=env)
    except Exception:
        return

    time.sleep(3)
    try:
        k32 = ctypes.windll.kernel32
        TH32CS_SNAPMODULE = 0x00000008
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, proc.pid)
        if snap == -1:
            return
        portable = str(output_dir).lower()

        class MODULEENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("th32ModuleID", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("GlblcntUsage", wintypes.DWORD),
                        ("ProccntUsage", wintypes.DWORD),
                        ("modBaseAddr", ctypes.POINTER(wintypes.BYTE)),
                        ("modBaseSize", wintypes.DWORD),
                        ("hModule", wintypes.HMODULE),
                        ("szModule", ctypes.c_char * 256),
                        ("szExePath", ctypes.c_char * 260)]

        me = MODULEENTRY32()
        me.dwSize = ctypes.sizeof(MODULEENTRY32)
        mingw_mods = []
        if k32.Module32First(snap, ctypes.byref(me)):
            while True:
                path = me.szExePath.decode("mbcs", "ignore").lower()
                if "mingw64\\bin" in path and portable not in path:
                    mingw_mods.append(me.szModule.decode("mbcs", "ignore").strip())
                if not k32.Module32Next(snap, ctypes.byref(me)):
                    break
        k32.CloseHandle(snap)

        def copy_chain(name, seen):
            if name.lower() in seen:
                return
            seen.add(name.lower())
            src = mingw_bin / name
            if not src.exists():
                return
            dst = output_dir / name
            if not dst.exists():
                shutil.copy2(src, dst)
                print(f"    runtime: {name}")
            for d in get_dll_deps(src):
                copy_chain(d, seen)

        seen = set()
        for name in set(mingw_mods):
            copy_chain(name, seen)
    except Exception:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def copy_core_files(build_dir, output_dir, src_dir):
    """Скопировать основные файлы (exe, libqmplay2, qt.conf, docs)."""
    gui_dir = build_dir / "src" / "gui"

    shutil.copy2(gui_dir / "QMPlay2.exe", output_dir / "QMPlay2.exe")
    print("  QMPlay2.exe")

    libqm = build_dir / "src" / "qmplay2" / "libqmplay2.dll"
    if libqm.exists():
        shutil.copy2(libqm, output_dir / "libqmplay2.dll")
        print("  libqmplay2.dll")

    qtconf = gui_dir / "qt.conf"
    if qtconf.exists():
        shutil.copy2(qtconf, output_dir / "qt.conf")
        print("  qt.conf")

    for f in ["AUTHORS", "ChangeLog", "LICENSE", "README.md"]:
        src_file = src_dir / f
        if src_file.exists():
            shutil.copy2(src_file, output_dir / f)
    print("  AUTHORS, ChangeLog, LICENSE, README.md")

    for f in ["llhls_fix.patch"]:
        src_file = src_dir / f
        if src_file.exists():
            shutil.copy2(src_file, output_dir / f)
            print(f"  {f}")


def copy_modules(build_dir, output_dir):
    """Скопировать модули."""
    modules_dir = output_dir / "modules"
    modules_dir.mkdir(exist_ok=True)

    modules_src = build_dir / "src" / "modules"
    count = 0
    for dll in modules_src.rglob("*.dll"):
        shutil.copy2(dll, modules_dir / dll.name)
        count += 1
    print(f"  modules/ ({count} файлов)")


def copy_translations(build_dir, output_dir):
    """Скопировать переводы."""
    lang_dir = output_dir / "lang"
    lang_dir.mkdir(exist_ok=True)

    lang_src = build_dir / "lang"
    count = 0
    for qm in lang_src.glob("*.qm"):
        shutil.copy2(qm, lang_dir / qm.name)
        count += 1
    print(f"  lang/ ({count} файлов)")


def deploy_qt_plugins(mingw_bin, output_dir):
    """Развернуть Qt DLL и плагины через windeployqt."""
    windeployqt = mingw_bin / "windeployqt.exe"
    if not windeployqt.exists():
        print("  WARNING: windeployqt не найден, Qt plugins не добавлены")
        return
    exe = (output_dir / "QMPlay2.exe").resolve()
    run([str(windeployqt), "--release", "--no-translations",
         "--no-opengl-sw", "--no-system-d3d-compiler", str(exe)])
    print("  Qt DLL + plugins (windeployqt)")


def create_clean_dir(output_dir):
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def verify(output_dir):
    """Проверить что всё на месте."""
    checks = [
        ("QMPlay2.exe", "exe"),
        ("qt.conf", "qt.conf"),
        ("libqmplay2.dll", "libqmplay2.dll"),
        ("modules/Extensions.dll", "модуль Extensions"),
        ("modules/FFmpeg.dll", "модуль FFmpeg"),
        ("lang/ru.qm", "русский язык"),
        ("platforms/qwindows.dll", "Qt platform plugin"),
    ]
    print("=== Проверка ===")
    all_ok = True
    for path, desc in checks:
        exists = (output_dir / path).exists()
        status = "OK" if exists else "MISSING"
        print(f"  {status}: {desc} ({path})")
        if not exists:
            all_ok = False
    dll_count = len(list(output_dir.glob("*.dll")))
    print(f"  DLL в корне: {dll_count}")
    return all_ok


def create_zip(output_dir, zip_path):
    """Упаковать в zip (файлы в корне архива, без вложенной папки)."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(output_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(output_dir)
                zf.write(file_path, str(arcname))
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    file_count = len(list(output_dir.rglob("*")))
    print(f"  {zip_path.name}: {size_mb:.1f} MB, {file_count} файлов")


def find_mingw_bin():
    """Найти mingw64/bin."""
    candidates = [
        Path(r"C:\msys64\mingw64\bin"),
        Path(r"C:\msys2\mingw64\bin"),
    ]
    for c in candidates:
        if c.exists():
            return c
    # поиск по PATH
    for p in os.environ.get("PATH", "").split(";"):
        if p.lower().endswith("mingw64\\bin") and Path(p).exists():
            return Path(p)
    return None


def main():
    parser = argparse.ArgumentParser(description="QMPlay2 Portable Deploy")
    parser.add_argument("--mingw-bin", type=Path, help="Путь к mingw64/bin")
    parser.add_argument("--output", type=Path, default=Path("QMPlay2_portable"),
                        help="Выходная директория (по умолчанию: QMPlay2_portable)")
    parser.add_argument("--skip-build", action="store_true",
                        help="Пропустить сборку (если уже собрано)")
    parser.add_argument("--zip", type=Path, default=Path("QMPlay2_LLHLS.zip"),
                        help="Путь к zip файлу")
    args = parser.parse_args()

    src_dir = Path(__file__).resolve().parent.parent
    os.chdir(src_dir)

    mingw_bin = args.mingw_bin or find_mingw_bin()
    if mingw_bin is None or not mingw_bin.exists():
        print("ERROR: mingw64/bin не найден. Укажите --mingw-bin PATH")
        sys.exit(1)
    print(f"mingw64/bin: {mingw_bin}")

    if not args.skip_build:
        print("Сборка не выполняется этим скриптом. Соберите через:")
        print("  cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release ...")
        print("  ninja -C build")
        sys.exit(1)

    build_dir = src_dir / "build"
    if not build_dir.exists():
        print("ERROR: build/ не найден. Сначала соберите проект.")
        sys.exit(1)

    print("\n=== Deploy ===")
    create_clean_dir(args.output)
    copy_core_files(build_dir, args.output, src_dir)
    copy_modules(build_dir, args.output)
    copy_translations(build_dir, args.output)
    deploy_qt_plugins(mingw_bin, args.output)
    print("  Сбор системных DLL (objdump)...")
    collect_system_dlls(args.output, mingw_bin)
    print("  Сбор рантайм-DLL (самодиагностика запуска)...")
    collect_runtime_dlls(args.output, mingw_bin)

    if not verify(args.output):
        print("\nERROR: Не все файлы на месте!")
        sys.exit(1)

    print()
    create_zip(args.output, args.zip)

    print(f"\nГотово! Portable сборка: {args.output}")
    print(f"Zip: {args.zip}")


if __name__ == "__main__":
    main()
