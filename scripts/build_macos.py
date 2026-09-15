#!/usr/bin/env python3
"""Build a locked, relocatable SDL3 MacFreeRDP.app and its installable DMG."""

from __future__ import annotations

import argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import plistlib

ROOT = Path(__file__).resolve().parent.parent
SYSTEM = ("/usr/lib/", "/System/Library/")


def run(*args, cwd=None, env=None, capture=False, timeout=None):
    """Run one checked command, preserving argument boundaries and build output."""
    command = [str(arg) for arg in args]
    print("$", shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout if capture else ""


def checkout(item):
    """Fetch an exact upstream commit, including its pinned submodule commits."""
    name, spec = item
    commit = spec["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"Invalid source commit: {name}")
    source = ROOT / "build/src" / f"{name}-{commit[:12]}"
    if not source.exists():
        source.mkdir(parents=True)
        run("git", "init", "-q", source)
        run("git", "-C", source, "remote", "add", "origin", spec["repository"])
    if not (source / ".ready").exists():
        run("git", "-C", source, "fetch", "--depth", "1", "origin", commit)
        run("git", "-C", source, "checkout", "--detach", "FETCH_HEAD")
        submodules = spec.get("submodules", [])
        if submodules:
            run("git", "-C", source, "submodule", "update", "--init", "--recursive", "--depth", "1", "--", *submodules)
        (source / ".ready").touch()
    actual = run("git", "-C", source, "rev-parse", "HEAD", capture=True).strip()
    if actual != commit or run("git", "-C", source, "status", "--porcelain", "--untracked-files=no", capture=True).strip():
        raise RuntimeError(f"Source checkout was modified: {source}")
    return name, source


def build_sources(lock, sources, arch, jobs):
    """Build all runtime libraries into a private prefix, excluding package-manager paths."""
    base = ROOT / "build" / arch
    fingerprint = hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()
    stamp = base / ".source-lock"
    if base.exists() and (not stamp.exists() or stamp.read_text() != fingerprint):
        shutil.rmtree(base)
    prefix = base / "prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    stamp.write_text(fingerprint)
    minimum = lock["target"]["minimum_macos"]
    env = os.environ.copy()
    for key in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "CMAKE_PREFIX_PATH",
                "CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS", "PKG_CONFIG_PATH", "DYLD_LIBRARY_PATH"):
        env.pop(key, None)
    env.update(CC="/usr/bin/clang", CXX="/usr/bin/clang++",
               MACOSX_DEPLOYMENT_TARGET=minimum,
               PKG_CONFIG_LIBDIR=f"{prefix}/lib/pkgconfig:{prefix}/share/pkgconfig",
               CFLAGS=f"-arch {arch} -mmacosx-version-min={minimum}",
               CXXFLAGS=f"-arch {arch} -mmacosx-version-min={minimum}",
               LDFLAGS=f"-arch {arch} -mmacosx-version-min={minimum} -Wl,-headerpad_max_install_names")
    common = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=ON",
              f"-DCMAKE_OSX_ARCHITECTURES={arch}", f"-DCMAKE_OSX_DEPLOYMENT_TARGET={minimum}",
              f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_INSTALL_LIBDIR=lib",
              f"-DCMAKE_PREFIX_PATH={prefix}",
              f"-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local;/opt/local;/Library/Frameworks;{Path.home()}/Library/Frameworks",
              "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF", "-DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON",
              "-DCMAKE_INSTALL_RPATH=@loader_path;@loader_path/../lib"]
    if arch != platform.machine():
        common += ["-DCMAKE_SYSTEM_NAME=Darwin", f"-DCMAKE_SYSTEM_PROCESSOR={arch}",
                   f"-DCMAKE_CROSSCOMPILING_EMULATOR=/usr/bin/arch;-{arch}"]

    def cmake(name, *options):
        """Configure, compile and install one CMake dependency or FreeRDP itself."""
        dest = base / name
        run("cmake", "-S", sources[name], "-B", dest, *common, *options, env=env)
        run("cmake", "--build", dest, "--parallel", jobs, env=env)
        run("cmake", "--install", dest, env=env)

    cmake("zlib", "-DZLIB_BUILD_TESTING=OFF")
    ssl_build = base / "openssl"
    ssl_build.mkdir(exist_ok=True)
    ssl_target = "darwin64-arm64-cc" if arch == "arm64" else "darwin64-x86_64-cc"
    ssl_args = ["perl", str(sources["openssl"] / "Configure"), ssl_target, f"--prefix={prefix}",
                "--libdir=lib", "--openssldir=/etc/ssl", "shared", "no-tests", "no-docs", "no-apps", "no-module",
                "no-legacy", "no-engine"]
    ssl_stamp = ssl_build / ".configure-args"
    ssl_config = json.dumps([ssl_args, env["CFLAGS"], env["LDFLAGS"]])
    if not ssl_stamp.exists() or ssl_stamp.read_text() != ssl_config:
        run(*ssl_args, cwd=ssl_build, env=env)
        ssl_stamp.write_text(ssl_config)
    run("make", f"-j{jobs}", "build_sw", cwd=ssl_build, env=env)
    run("make", "install_sw", cwd=ssl_build, env=env)
    # cJSON 1.7.19 declares CMake 3.0; CMake 4 requires a policy floor of 3.5.
    cmake("cjson", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DENABLE_CJSON_TEST=OFF", "-DENABLE_CJSON_UTILS=OFF")
    cmake("uriparser", "-DURIPARSER_BUILD_DOCS=OFF", "-DURIPARSER_BUILD_TESTS=OFF", "-DURIPARSER_BUILD_TOOLS=OFF")
    cmake("opus", "-DOPUS_BUILD_SHARED_LIBRARY=ON", "-DOPUS_BUILD_TESTING=OFF", "-DOPUS_BUILD_PROGRAMS=OFF")
    cmake("libusb", "-DLIBUSB_BUILD_SHARED_LIBS=ON", "-DLIBUSB_BUILD_TESTING=OFF", "-DLIBUSB_BUILD_EXAMPLES=OFF")
    h264_build = base / "openh264"
    command = ["meson", "setup", h264_build, sources["openh264"], f"--prefix={prefix}", "--libdir=lib",
               "--buildtype=release", "--default-library=shared", "--wrap-mode=nodownload", "-Dtests=disabled"]
    if arch != platform.machine():
        cross_file = base / "meson-cross.ini"
        cpu = "aarch64" if arch == "arm64" else "x86_64"
        cross_file.write_text(
            "[binaries]\n"
            f"c = ['/usr/bin/clang', '-arch', '{arch}']\n"
            f"cpp = ['/usr/bin/clang++', '-arch', '{arch}']\n"
            "ar = '/usr/bin/ar'\nstrip = '/usr/bin/strip'\n"
            f"pkg-config = '{shutil.which('pkg-config')}'\n"
            f"exe_wrapper = ['/usr/bin/arch', '-{arch}']\n"
            "[host_machine]\nsystem = 'darwin'\n"
            f"cpu_family = '{cpu}'\ncpu = '{cpu}'\nendian = 'little'\n"
            "[properties]\nneeds_exe_wrapper = true\n")
        command += ["--cross-file", cross_file]
    if (h264_build / "build.ninja").exists():
        command.append("--reconfigure")
    run(*command, env=env)
    run("meson", "compile", "-C", h264_build, "-j", jobs, env=env)
    run("meson", "install", "-C", h264_build, env=env)
    cmake("sdl", "-DSDL_TESTS=OFF", "-DSDL_TEST_LIBRARY=OFF", "-DSDL_EXAMPLES=OFF",
          "-DSDL_SHARED=ON", "-DSDL_STATIC=OFF", "-DSDL_FRAMEWORK=OFF")
    cmake("sdl_ttf", "-DSDLTTF_VENDORED=ON", "-DSDLTTF_HARFBUZZ=ON", "-DSDLTTF_FREETYPE=ON",
          "-DSDLTTF_SAMPLES=OFF", "-DSDLTTF_TESTS=OFF", "-DSDLTTF_PLUTOSVG=OFF",
          "-DFT_DISABLE_ZLIB=ON", "-DFT_DISABLE_BZIP2=ON", "-DFT_DISABLE_PNG=ON", "-DFT_DISABLE_BROTLI=ON")
    cmake("freerdp", "-DWITH_CLIENT=ON", "-DWITH_CLIENT_MAC=OFF", "-DWITH_CLIENT_SDL=ON",
          "-DWITH_CLIENT_SDL3=ON", "-DWITH_CLIENT_SDL2=OFF", "-DCMAKE_DISABLE_FIND_PACKAGE_SDL2=ON", "-DSDL2_FOUND=OFF",
          "-DWITH_CLIENT_SDL_VERSIONED=ON",
          "-DWITH_BINARY_VERSIONING=OFF", "-DWITH_SDL_IMAGE_DIALOGS=OFF", "-DWITH_SDL_LINK_SHARED=ON",
          "-DWITH_SERVER=OFF", "-DWITH_SAMPLE=OFF", "-DWITH_X11=OFF", "-DWITH_WAYLAND=OFF",
          "-DWITH_FUSE=OFF", "-DWITH_WEBVIEW=OFF", "-DWITH_MANPAGES=OFF", "-DWITH_CCACHE=OFF",
          "-DBUILD_TESTING=OFF", "-DWITH_WINPR_TOOLS=OFF", "-DWITH_PKCS11=OFF", "-DWITH_KRB5=OFF",
          "-DWITH_FFMPEG=OFF", "-DWITH_SWSCALE=OFF", "-DWITH_OPENH264=ON", "-DWITH_OPENH264_LOADING=OFF",
          "-DWITH_OPUS=ON", "-DWITH_FAAC=OFF", "-DWITH_FAAD2=OFF", "-DWITH_FDK_AAC=OFF",
          "-DWITH_CAIRO=OFF", "-DWITH_CJSON_REQUIRED=ON", "-DWITH_INTERNAL_MD4=ON",
          "-DWITH_INTERNAL_MD5=ON", "-DWITH_INTERNAL_RC4=ON", "-DWITH_ABSOLUTE_PLUGIN_LOAD_PATHS=OFF",
          "-DWITH_FREERDP_DEPRECATED_COMMANDLINE=ON", f"-DOPENSSL_ROOT_DIR={prefix}")
    return prefix


def dependencies(path, arch=None):
    """Read Mach-O load commands without confusing spaces in the filename."""
    selection = ["-arch", arch] if arch else []
    lines = run("/usr/bin/otool", *selection, "-L", path, capture=True).splitlines()[1:]
    return [line.strip().split(" (compatibility version", 1)[0] for line in lines if line.startswith("\t")]


def rpaths(path, arch=None):
    """Read the LC_RPATH entries used by the dynamic loader."""
    selection = ["-arch", arch] if arch else []
    text = run("/usr/bin/otool", *selection, "-l", path, capture=True)
    return re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", text)


def package(lock, sources, prefix, arch):
    """Copy the client and its transitive libraries, then rewrite and verify load paths."""
    app = ROOT / "build" / arch / "MacFreeRDP.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    frameworks = contents / "Frameworks"
    resources = contents / "Resources"
    for part in (contents / "MacOS", frameworks, resources):
        part.mkdir(parents=True, exist_ok=True)
    executable = prefix / "bin/sdl3-freerdp"
    nodes = {executable.resolve(): contents / "MacOS/sdl3-freerdp"}
    queue = [executable.resolve()]
    rewrites = {}
    names = {}
    for source in queue:
        destination = nodes[source]
        shutil.copy2(source, destination)
        rewrites[destination] = []
        for dep in dependencies(source):
            if dep.startswith(SYSTEM):
                continue
            if dep.startswith("@rpath/"):
                candidates = [prefix / "lib" / dep.removeprefix("@rpath/")]
            elif dep.startswith("@loader_path/"):
                candidates = [source.parent / dep.removeprefix("@loader_path/")]
            elif dep.startswith("@executable_path/"):
                candidates = [executable.parent / dep.removeprefix("@executable_path/")]
            else:
                candidates = [Path(dep)]
            resolved = next((p.resolve() for p in candidates if p.is_file()), None)
            if resolved is None or not resolved.is_relative_to(prefix.resolve()):
                raise RuntimeError(f"External or missing runtime dependency: {source}: {dep}")
            if resolved == source:
                continue  # dylib's own LC_ID_DYLIB
            if resolved not in nodes:
                name = resolved.name
                if name in names and names[name] != resolved:
                    raise RuntimeError(f"Library name collision: {name}")
                names[name] = resolved
                nodes[resolved] = frameworks / name
                queue.append(resolved)
            rewrites[destination].append((dep, nodes[resolved]))
    for source, destination in nodes.items():
        for old, target in rewrites[destination]:
            run("/usr/bin/install_name_tool", "-change", old, f"@rpath/{target.name}", destination)
        if destination.parent == frameworks:
            run("/usr/bin/install_name_tool", "-id", f"@rpath/{destination.name}", destination)
        for old in dict.fromkeys(rpaths(destination)):
            run("/usr/bin/install_name_tool", "-delete_rpath", old, destination)
        run("/usr/bin/install_name_tool", "-add_rpath", "@loader_path/../Frameworks", destination)
    version = lock["sources"]["freerdp"]["version"]
    numeric = version.split("-", 1)[0]
    info = dict(CFBundleExecutable="sdl3-freerdp", CFBundleName="MacFreeRDP", CFBundleDisplayName="MacFreeRDP",
                CFBundleIdentifier="com.lierfang.macfreerdp", CFBundlePackageType="APPL",
                CFBundleInfoDictionaryVersion="6.0", CFBundleShortVersionString=numeric, CFBundleVersion=numeric,
                CFBundleIconFile="FreeRDP.icns", LSMinimumSystemVersion=lock["target"]["minimum_macos"],
                NSHighResolutionCapable=True, NSPrincipalClass="NSApplication",
                NSMicrophoneUsageDescription="将麦克风重定向到远程桌面。",
                NSCameraUsageDescription="将摄像头重定向到远程桌面。",
                FreeRDPSourceCommit=lock["sources"]["freerdp"]["commit"])
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    (contents / "PkgInfo").write_bytes(b"APPL????")
    shutil.copy2(sources["freerdp"] / "client/Mac/cli/FreeRDP.icns", resources)
    (resources / "build.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    for name, source in sources.items():
        for pattern in ("LICENSE*", "COPYING*", "NOTICE*", "COPYRIGHT*", "external/*/LICENSE*", "external/*/COPYING*",
                        "external/freetype/docs/FTL.TXT", "external/freetype/docs/GPLv2.TXT",
                        "client/SDL/SDL3/dialogs/font/OFL.txt", "resources/font/OFL.txt"):
            for license_path in source.glob(pattern):
                if license_path.is_file():
                    target = resources / "licenses" / name / license_path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(license_path, target)
    sign_bundle(app)
    verify_bundle(app, [arch])
    return app


def macho_files(app):
    """Locate actual executable/library files, including universal Mach-O binaries."""
    magic = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    result = []
    for path in app.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in magic:
                    result.append(path)
    return result


def sign_bundle(app):
    """Sign all architecture slices after merging, working from libraries to the app."""
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    main_executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    identity = os.environ.get("DEVELOPER_ID", "-")
    signing = ["--force", "--sign", identity]
    if identity != "-":
        signing += ["--timestamp", "--options", "runtime"]
    for path in sorted(macho_files(app), key=lambda p: len(p.parts), reverse=True):
        if path != main_executable:
            run("/usr/bin/codesign", *signing, path)
    run("/usr/bin/codesign", *signing, "--entitlements", ROOT / "packaging/entitlements.plist", app)


def verify_bundle(app, architectures):
    """Require every binary and each library load path to support every expected architecture."""
    frameworks = app / "Contents/Frameworks"
    binaries = macho_files(app)
    if app / "Contents/MacOS/sdl3-freerdp" not in binaries:
        raise RuntimeError(f"Missing SDL3 executable: {app}")
    for path in binaries:
        if set(run("/usr/bin/lipo", "-archs", path, capture=True).split()) != set(architectures):
            raise RuntimeError(f"Wrong architecture: {path}")
        for arch in architectures:
            for dep in dependencies(path, arch):
                if dep.startswith(SYSTEM):
                    continue
                if not dep.startswith("@rpath/") or frameworks / dep.removeprefix("@rpath/") not in binaries:
                    raise RuntimeError(f"Unbundled dependency ({arch}): {path}: {dep}")
            if rpaths(path, arch) != ["@loader_path/../Frameworks"]:
                raise RuntimeError(f"Non-relocatable RPATH ({arch}): {path}")
    run("/usr/bin/plutil", "-lint", app / "Contents/Info.plist")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
    print(f"Verified {len(binaries)} Mach-O files, each containing {', '.join(architectures)}", flush=True)


def merge_bundles(lock, arm_app, intel_app):
    """Merge matching app trees; fail on missing binaries, incompatible versions or different resources."""
    for app, arch in ((arm_app, "arm64"), (intel_app, "x86_64")):
        verify_bundle(app, [arch])
        if json.loads((app / "Contents/Resources/build.lock.json").read_text()) != lock:
            raise RuntimeError(f"Bundle was built from a different source lock: {app}")

    def inventory(app):
        """List bundle files while excluding the per-build code signature envelope."""
        return {p.relative_to(app): p for p in app.rglob("*")
                if (p.is_file() or p.is_symlink()) and "_CodeSignature" not in p.relative_to(app).parts}

    arm_files, intel_files = inventory(arm_app), inventory(intel_app)
    if arm_files.keys() != intel_files.keys():
        raise RuntimeError(f"Architecture bundle contents differ: {sorted(arm_files.keys() ^ intel_files.keys())}")
    code = {p.relative_to(arm_app) for p in macho_files(arm_app)}
    if code != {p.relative_to(intel_app) for p in macho_files(intel_app)}:
        raise RuntimeError("Architecture bundles contain different executable/library sets")
    for relative in arm_files.keys() - code:
        left, right = arm_files[relative], intel_files[relative]
        if left.is_symlink() or right.is_symlink():
            if not (left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)):
                raise RuntimeError(f"Architecture symlinks differ: {relative}")
        elif left.read_bytes() != right.read_bytes():
            raise RuntimeError(f"Architecture resources differ: {relative}")
    app = ROOT / "build/universal/MacFreeRDP.app"
    if app.exists():
        shutil.rmtree(app)
    app.parent.mkdir(parents=True, exist_ok=True)
    run("/usr/bin/ditto", arm_app, app)
    for relative in sorted(code):
        target = app / relative
        merged = target.with_name(target.name + ".universal")
        run("/usr/bin/lipo", "-create", arm_files[relative], intel_files[relative], "-output", merged)
        merged.replace(target)
    sign_bundle(app)
    verify_bundle(app, ["arm64", "x86_64"])
    return app


def verify_launch(app, prefixes, version, architectures):
    """Exercise each runnable architecture after relocation, hiding all private build prefixes."""
    prefixes = [(p, p.with_name("prefix-relocation-test")) for p in prefixes if p.exists()]
    for prefix, hidden in prefixes:
        if hidden.exists():
            raise RuntimeError(f"Restore leftover relocation-test directory first: {hidden}")
    with tempfile.TemporaryDirectory(prefix="MacFreeRDP check ") as directory:
        relocated = Path(directory) / "Renamed FreeRDP.app"
        run("/usr/bin/ditto", app, relocated)
        moved = []
        try:
            for prefix, hidden in prefixes:
                prefix.rename(hidden)
                moved.append((prefix, hidden))
            environment = {key: os.environ[key] for key in ("HOME", "TMPDIR", "USER", "LOGNAME") if key in os.environ}
            environment.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", DYLD_PRINT_LIBRARIES="1")
            runnable = []
            for arch in architectures:
                probe = subprocess.run(["/usr/bin/arch", f"-{arch}", "/usr/bin/true"], capture_output=True)
                if probe.returncode == 0:
                    runnable.append(arch)
                else:
                    print(f"SKIP runtime {arch}: this host cannot execute it; use its native CI runner", flush=True)
            if not runnable:
                raise RuntimeError("This host cannot execute any requested architecture")
            for arch in runnable:
                for argument in ("/version", "/help", "/buildconfig"):
                    result = subprocess.run(["/usr/bin/arch", f"-{arch}", str(relocated / "Contents/MacOS/sdl3-freerdp"), argument],
                                            env=environment, cwd=directory, capture_output=True, text=True, timeout=30)
                    output = result.stdout + result.stderr
                    (ROOT / "build" / f"smoke-{arch}-{argument[1:]}.log").write_text(output)
                    if result.returncode or (argument == "/version" and version not in output):
                        raise RuntimeError(f"Relocated client failed {argument}: {result.returncode}\n{output}")
                    if argument == "/buildconfig":
                        for feature in ("WITH_CLIENT_SDL3", "WITH_OPENH264", "WITH_OPUS"):
                            if not re.search(rf"\b{feature}=(ON|TRUE|1)\b", output):
                                raise RuntimeError(f"Required build feature missing: {feature}")
                        if not re.search(r"\bWITH_FFMPEG=(OFF|FALSE|0)\b", output):
                            raise RuntimeError("Unexpected FFmpeg dependency")
                    for line in result.stderr.splitlines():
                        if line.startswith("dyld[") and re.search(r"/(opt/homebrew|opt/local|usr/local)/", line):
                            raise RuntimeError(f"External library loaded: {line}")
                    print(f"PASS relocated app {arch} {argument}", flush=True)
        finally:
            for prefix, hidden in reversed(moved):
                hidden.rename(prefix)


@contextmanager
def open_app(source):
    """Open an app directly or mount a checked DMG until its consumer finishes."""
    source = source.resolve()
    if source.suffix.lower() != ".dmg":
        yield source
        return
    run("/usr/bin/hdiutil", "verify", source)
    with tempfile.TemporaryDirectory(prefix="MacFreeRDP mount ") as directory:
        run("/usr/bin/hdiutil", "attach", source, "-readonly", "-nobrowse", "-noautoopen",
            "-mountpoint", directory)
        try:
            app = Path(directory) / "MacFreeRDP.app"
            applications = Path(directory) / "Applications"
            if not app.is_dir() or not applications.is_symlink() or os.readlink(applications) != "/Applications":
                raise RuntimeError(f"DMG must contain MacFreeRDP.app and an Applications shortcut: {source}")
            yield app
        finally:
            run("/usr/bin/hdiutil", "detach", directory)


def archive_app(app, label):
    """Publish a compressed DMG with an Applications shortcut and SHA-256 sidecar."""
    destination = ROOT / "dist/MacFreeRDP.app"
    destination.parent.mkdir(exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    run("/usr/bin/ditto", app, destination)
    archive = ROOT / "dist" / f"MacFreeRDP-macos-{label}.dmg"
    with tempfile.TemporaryDirectory(prefix="dmg-", dir=ROOT / "build") as directory:
        staging = Path(directory) / "staging"
        staging.mkdir()
        run("/usr/bin/ditto", destination, staging / destination.name)
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
        image = Path(directory) / archive.name
        run("/usr/bin/hdiutil", "create", "-volname", "MacFreeRDP", "-srcfolder", staging,
            "-fs", "HFS+", "-format", "UDZO", "-imagekey", "zlib-level=9", image)
        run("/usr/bin/hdiutil", "verify", image)
        image.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".dmg.sha256").write_text(f"{digest}  {archive.name}\n")
    print(f"Application: {destination}\nDMG: {archive}\nSHA256: {digest}")


def main():
    """Build a universal app by default, or assemble/verify the native CI build results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("universal", "arm64", "x86_64"), default="universal")
    parser.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument("--package-only", action="store_true", help="repackage an existing private prefix")
    parser.add_argument("--merge-from", nargs=2, type=Path, metavar=("ARM_INPUT", "INTEL_INPUT"), help="merge native CI apps or DMGs")
    parser.add_argument("--verify-only", type=Path, metavar="APP_OR_DMG", help="verify and launch the downloaded universal app or DMG")
    args = parser.parse_args()
    (ROOT / "build").mkdir(exist_ok=True)
    build_lock = (ROOT / "build/.builder.lock").open("w")
    try:
        fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another build is already using this project directory")
    if sys.platform != "darwin" or args.jobs < 1:
        parser.error("Use a macOS host and a positive job count")
    if args.merge_from and (args.verify_only or args.package_only or args.arch != "universal"):
        parser.error("--merge-from cannot be combined with other build/verification modes")
    architectures = ["arm64", "x86_64"] if args.arch == "universal" else [args.arch]
    lock = json.loads((ROOT / "build.lock.json").read_text())
    if lock["schema"] != 1:
        parser.error("Unsupported build lock schema")
    version = lock["sources"]["freerdp"]["version"]
    prefixes = [ROOT / "build" / arch / "prefix" for arch in ("arm64", "x86_64")]
    if args.verify_only:
        with open_app(args.verify_only) as app:
            verify_bundle(app, ["arm64", "x86_64"])
            verify_launch(app, prefixes, version, architectures)
        return
    if args.merge_from:
        with ExitStack() as stack:
            inputs = [stack.enter_context(open_app(p)) for p in args.merge_from]
            app = merge_bundles(lock, *inputs)
        verify_launch(app, prefixes, version, architectures)
        archive_app(app, "universal")
        return
    for command in ("git", "cmake", "ninja", "meson", "pkg-config", "perl", "make", "clang", "xcodebuild"):
        if shutil.which(command) is None:
            parser.error(f"Missing build tool: {command}")
    if "x86_64" in architectures and shutil.which("nasm") is None:
        parser.error("Intel builds require nasm for OpenH264")
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = dict(pool.map(checkout, lock["sources"].items()))
    apps = []
    for arch in architectures:
        prefix = ROOT / "build" / arch / "prefix"
        if not args.package_only:
            prefix = build_sources(lock, sources, arch, args.jobs)
        apps.append(package(lock, sources, prefix, arch))
    app = merge_bundles(lock, *apps) if args.arch == "universal" else apps[0]
    verify_launch(app, prefixes, version, architectures)
    archive_app(app, args.arch)


if __name__ == "__main__":
    main()
