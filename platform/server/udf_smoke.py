"""UDF D1-D5/G5 垂直切片：wmake 编译 → 动态加载 → functions 集成执行。"""
from pathlib import Path
import shutil
import subprocess

SERVER = Path(__file__).resolve().parent
EXAMPLE = SERVER / "examples" / "udf"
CASE_SOURCE = SERVER / "data" / "files" / "workspaces" / "1" / "cases" / "1" / "C-run" / "case-1"
WORK = Path("D:/tmp/vibeflow-udf-g5")
PRELOAD = ("/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libOpenFOAM.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libfiniteVolume.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libmeshTools.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libfileFormats.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/libsurfMesh.so:"
           "/usr/lib/openfoam/openfoam2406/platforms/linux64GccDPInt32Opt/lib/sys-openmpi/libPstream.so")


def wsl_path(path: Path) -> str:
    text = path.as_posix()
    if len(text) > 1 and text[1] == ":":
        return f"/mnt/{text[0].lower()}{text[2:]}"
    return text


def run(command: str) -> subprocess.CompletedProcess:
    # 不用 WSL 默认 shell，避免在 source v2406 前展开 $LD_LIBRARY_PATH。
    return subprocess.run(
        ["wsl", "-d", "vibeflow-linux", "--exec", "bash", "-lc", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
    )


def require_ok(result: subprocess.CompletedProcess, stage: str):
    if result.returncode:
        raise RuntimeError(f"{stage} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    print(f"{stage} [OK]")


def main():
    if not EXAMPLE.is_dir():
        raise FileNotFoundError(EXAMPLE)
    if not CASE_SOURCE.is_dir():
        raise FileNotFoundError(CASE_SOURCE)
    if WORK.exists() and WORK.name == "vibeflow-udf-g5":
        shutil.rmtree(WORK)
    (WORK / "src").mkdir(parents=True)
    shutil.copytree(EXAMPLE, WORK / "src", dirs_exist_ok=True)
    shutil.copytree(CASE_SOURCE / "system", WORK / "case" / "system")
    shutil.copytree(CASE_SOURCE / "constant", WORK / "case" / "constant")
    shutil.copytree(CASE_SOURCE / "0", WORK / "case" / "0")

    src = wsl_path(WORK / "src")
    require_ok(run(f"source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd {src} && wmake libso"), "D4 wmake 编译")
    require_ok(run(f"source /usr/lib/openfoam/openfoam2406/etc/bashrc && export LD_PRELOAD={PRELOAD} && python3 -c 'import ctypes; ctypes.CDLL(\"{src}/libmyFunctionObject.so\")'"), "D4 动态加载")

    control = WORK / "case" / "system" / "controlDict"
    control.write_text(
        control.read_text(encoding="utf-8")
        + '\nfunctions\n{\n    myFunctionObject\n    {\n        type myFunctionObject;\n        libs ("libmyFunctionObject.so");\n    }\n}\n',
        encoding="utf-8",
    )
    result = run(
        f"source /usr/lib/openfoam/openfoam2406/etc/bashrc && export LD_PRELOAD={PRELOAD} && "
        f"export LD_LIBRARY_PATH={src}:$LD_LIBRARY_PATH && cd {wsl_path(WORK / 'case')} && icoFoam"
    )
    output = result.stdout + result.stderr
    if "P0_UDF_OK" not in output:
        raise RuntimeError(f"UDF 未在算例中执行: {output[-4000:]}")
    require_ok(result, "D5 functions 集成执行")
    assert (WORK / "case" / "0.1" / "U").is_file(), "未生成预期 0.1 时刻"
    print("D5 算例产生新时刻 [OK]")
    print("\n=== UDF D1-D5/G5 垂直切片通过 ===")


if __name__ == "__main__":
    main()
