"""A web page that hijacks the agent must not be able to read a secret and ship it out
without the user being asked first."""
import pytest

from r2.tools import files

from r2.types import Risk


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\x\proj\.env",
        r"C:\Users\x\proj\.env.local",
        "C:/Users/x/Local-LLm/data/tokens.json",
        "C:/Users/x/Local-LLm/data/overlay.token",
        r"C:\Users\x\.ssh\id_rsa",
        r"C:\Users\x\.ssh\config",
        r"C:\Users\x\.aws\credentials",
        r"D:\keys\server.pem",
        r"D:\vault\passwords.kdbx",
        r"C:\Users\x\.git-credentials",
        r"C:\Users\x\.npmrc",
        "C:/Users/x/Local-LLm/data/browser-profile/Default/Cookies",
        r"C:\Users\x\AppData\Local\Google\Chrome\User Data\Default\Login Data",
    ],
)
def test_reading_secret_files_needs_approval(path):
    info = files.classify_read({"path": path})
    assert info.risk is Risk.CONFIRM and info.action_type == "sensitive_read"


@pytest.mark.parametrize(
    "path",
    [r"C:\Users\x\Documents\notes.txt", "report.pdf", "C:/proj/src/main.py", "environment.md"],
)
def test_ordinary_files_read_without_asking(path):
    assert files.classify_read({"path": path}).risk is Risk.SAFE


def test_classify_read_survives_missing_arguments():
    # no path: nothing sensitive to protect, and the tool itself will fail on its own
    assert files.classify_read({}).risk is Risk.SAFE
