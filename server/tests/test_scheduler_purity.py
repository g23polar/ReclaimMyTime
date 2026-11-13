from pathlib import Path
from subprocess import run


def test_scheduler_purity_import_contract() -> None:
    server_dir = Path(__file__).resolve().parents[1]

    result = run(["lint-imports"], cwd=server_dir, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
