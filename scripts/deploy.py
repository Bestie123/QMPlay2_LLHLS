#!/usr/bin/env python3
"""
QMPlay2 Portable Deploy Script

Собирает QMPlay2 из исходников и создаёт portable-сборку со всеми зависимостями.
Использует objdump для рекурсивного поиска DLL-зависимостей.

Использование:
    python scripts/deploy.py [--mingw-bin PATH] [--output PATH] [--skip-build]
"""

import argparse
import os
import shutil
import subprocess
import sys
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
            capture_output=True, text=True, timeout=10
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
            if f.name.lower() == name_lower:
                return f
    return None


def collect_all_deps(exe_path, search_dirs, output_dir, seen=None):
    """Рекурсивно собрать все зависимости через objdump."""
    if seen is None:
        seen = set()

    deps = get_dll_deps(exe_path)
    copied = 0

    for dep in deps:
        dep_lower = dep.lower()
        if dep_lower in seen:
            continue
        seen.add(dep_lower)

        src = find_dll(dep, search_dirs)
        if src is None:
            continue

        dst = output_dir / dep
        if dst.exists():
            continue

        shutil.copy2(src, dst)
        copied += 1

        # Рекурсивно проверяем зависимости этой DLL
        sub_deps = collect_all_deps(dst, search_dirs, output_dir, seen)
        copied += sub_deps

    return copied


def find_mingw_bin():
    """Найти mingw64/bin автоматически."""
    candidates = [
        Path("C:/msys64/mingw64/bin"),
        Path("C:/msys64/usr/bin"),
        Path(os.environ.get("MINGW_PREFIX", "") + "/bin"),
    ]
    for p in candidates:
        if p.exists() and (p / "gcc.exe").exists():
            return p
    # Fallback: ищем через which
    try:
        result = subprocess.run(["where", "gcc"], capture_output=True, text=True)
        if result.returncode == 0:
            gcc_path = Path(result.stdout.strip().splitlines()[0])
            return gcc_path.parent
    except Exception:
        pass
    return None


def build(mingw_bin):
    """Собрать QMPlay2."""
    src_dir = Path(__file__).resolve().parent.parent
    build_dir = src_dir / "build"

    print("=== Конфигурация cmake ===")
    run([
        "cmake", "-S", str(src_dir), "-B", str(build_dir),
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DUSE_VULKAN=OFF",
        "-DUSE_PORTAUDIO=OFF",
        "-DUSE_QML=OFF",
    ], cwd=str(src_dir))

    print("=== Сборка ninja ===")
    run(["ninja"], cwd=str(build_dir))

    return build_dir


def create_clean_dir(output_path):
    """Создать чистую выходную директорию."""
    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True)
    print(f"Создана: {output_path}")


def copy_core_files(build_dir, output_dir, src_dir):
    """Скопировать основные файлы."""
    gui_dir = build_dir / "src" / "gui"

    # exe
    shutil.copy2(gui_dir / "QMPlay2.exe", output_dir / "QMPlay2.exe")
    print("  QMPlay2.exe")

    # libqmplay2.dll
    libqm = build_dir / "src" / "qmplay2" / "libqmplay2.dll"
    if libqm.exists():
        shutil.copy2(libqm, output_dir / "libqmplay2.dll")
        print("  libqmplay2.dll")

    # qt.conf
    qtconf = gui_dir / "qt.conf"
    if qtconf.exists():
        shutil.copy2(qtconf, output_dir / "qt.conf")
        print("  qt.conf")

    # Авторские права
    for f in ["AUTHORS", "ChangeLog", "LICENSE", "README.md"]:
        src_file = src_dir / f
        if src_file.exists():
            shutil.copy2(src_file, output_dir / f)
    print("  AUTHORS, ChangeLog, LICENSE, README.md")

    # Документация
    for f in ["BUGFIX_LLHLS_Recording.md", "llhls_fix.patch"]:
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
    """Развернуть Qt plugins через windeployqt."""
    windeployqt = mingw_bin / "windeployqt.exe"
    if not windeployqt.exists():
        print("  WARNING: windeployqt не найден, пропускаю Qt plugins")
        return

    exe = output_dir / "QMPlay2.exe"
    run([str(windeployqt), "--release", "--no-translations",
         "--no-opengl-sw", "--no-system-d3d-compiler", str(exe)],
        cwd=str(output_dir))
    print("  Qt plugins (windeployqt)")


def deploy_from_build(build_dir, output_dir, src_dir):
    """Скопировать всё из gui/ (где ninja положил файлы) + дополнения.

    Ninja + cmake уже положили в gui/:
    - QMPlay2.exe
    - Все DLL (libqmplay2, Qt, FFmpeg, системные)
    - platforms/, styles/, imageformats/, iconengines/, tls/
    - modules/
    - lang/
    - qt.conf

    Нам нужно только:
    1. Скопировать gui/ contents
    2. Добавить AUTHORS, ChangeLog, LICENSE, README.md
    3. Добавить документацию
    """
    gui_dir = build_dir / "src" / "gui"

    # Копируем ВСЁ из gui/ кроме cmake артефактов
    print("  Копирование gui/ contents...")
    skip = {"CMakeFiles", "QMPlay2_autogen", "cmake_install.cmake", "build.ninja",
            ".ninja_log", ".ninja_deps", "CMakeCache.txt"}
    count = 0
    for item in gui_dir.iterdir():
        if item.name in skip or item.name.endswith(".cmake"):
            continue
        dst = output_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
        count += 1
    print(f"  Скопировано: {count} элементов из gui/")

    # Авторские права
    for f in ["AUTHORS", "ChangeLog", "LICENSE", "README.md"]:
        src_file = src_dir / f
        if src_file.exists():
            shutil.copy2(src_file, output_dir / f)

    # Документация
    for f in ["BUGFIX_LLHLS_Recording.md", "llhls_fix.patch"]:
        src_file = src_dir / f
        if src_file.exists():
            shutil.copy2(src_file, output_dir / f)


def create_zip(output_dir, zip_path):
    """Упаковать в zip (файлы в корне архива, без вложенной папки)."""
    import zipfile
    print(f"=== Упаковка {zip_path.name} ===")
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(output_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(output_dir)
                zf.write(file_path, arcname)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    file_count = len(list(output_dir.rglob("*")))
    print(f"  {zip_path.name}: {size_mb:.1f} MB, {file_count} файлов")


def verify(output_dir):
    """Проверить что всё на месте."""
    checks = [
        ("QMPlay2.exe", "exe"),
        ("qt.conf", "qt.conf"),
        ("libqmplay2.dll", "libqmplay2.dll"),
        ("modules/Extensions.dll", "модуль"),
        ("modules/FFmpeg.dll", "модуль"),
        ("lang/ru.qm", "русский язык"),
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
    print(f"  DLL: {dll_count}")

    return all_ok


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

    # Определяем mingw64/bin
    mingw_bin = args.mingw_bin or find_mingw_bin()
    if mingw_bin is None or not mingw_bin.exists():
        print("ERROR: mingw64/bin не найден. Укажите --mingw-bin PATH")
        sys.exit(1)
    print(f"mingw64/bin: {mingw_bin}")

    # Сборка
    if not args.skip_build:
        build_dir = build(mingw_bin)
    else:
        build_dir = src_dir / "build"
        if not build_dir.exists():
            print("ERROR: build/ не найден. Уберите --skip-build")
            sys.exit(1)

    # Deploy
    print("\n=== Deploy ===")
    create_clean_dir(args.output)
    deploy_from_build(build_dir, args.output, src_dir)

    # Проверка
    if not verify(args.output):
        print("\nERROR: Не все файлы на месте!")
        sys.exit(1)

    # Zip
    print()
    create_zip(args.output, args.zip)

    print(f"\nГотово! Portable сборка: {args.output}")
    print(f"Zip: {args.zip}")


if __name__ == "__main__":
    main()
