import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REQUIRED_CREDENTIAL_KEYS = (
    "MAX_TOKEN",
    "MAX_DEVICE_ID",
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
)

STATE_FILE_NAME = ".max2tg-runner.json"
CONSOLE_LOG_NAME = "max2tg-console.log"


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def render_env_text(base_text: str, updates: dict[str, str]) -> str:
    lines = base_text.splitlines()
    rendered: list[str] = []
    replaced: set[str] = set()

    for raw_line in lines:
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            rendered.append(raw_line)
            continue

        key, _ = raw_line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in updates:
            rendered.append(f"{normalized_key}={updates[normalized_key]}")
            replaced.add(normalized_key)
            continue

        rendered.append(raw_line)

    missing_keys = [key for key in updates if key not in replaced]
    if missing_keys and rendered and rendered[-1] != "":
        rendered.append("")
    for key in missing_keys:
        rendered.append(f"{key}={updates[key]}")

    return "\n".join(rendered).rstrip("\n") + "\n"


def validate_required_credentials(values: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_CREDENTIAL_KEYS if not values.get(key)]
    if missing:
        raise ValueError(
            "В .env не заполнены обязательные значения: " + ", ".join(missing)
        )

    tg_chat_id = values["TG_CHAT_ID"]
    try:
        int(tg_chat_id)
    except ValueError as exc:
        raise ValueError("TG_CHAT_ID должен быть целым числом.") from exc


def ask_yes_no(prompt: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{prompt} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "д", "да"}:
            return True
        if answer in {"n", "no", "н", "нет"}:
            return False
        print("Введите y/yes/да или n/no/нет.")


def prompt_required_credentials() -> dict[str, str]:
    prompts = (
        ("MAX_TOKEN", "MAX_TOKEN (__oneme_auth из web.max.ru)"),
        ("MAX_DEVICE_ID", "MAX_DEVICE_ID (__oneme_device_id из web.max.ru)"),
        ("TG_BOT_TOKEN", "TG_BOT_TOKEN (токен от @BotFather)"),
        ("TG_CHAT_ID", "TG_CHAT_ID (ваш chat id в Telegram)"),
    )

    print("Введите обязательные креды для Max и Telegram.")
    values: dict[str, str] = {}
    for key, label in prompts:
        while True:
            value = input(f"{label}: ").strip()
            if not value:
                print("Значение не может быть пустым.")
                continue
            if key == "TG_CHAT_ID":
                try:
                    int(value)
                except ValueError:
                    print("TG_CHAT_ID должен быть целым числом.")
                    continue
            values[key] = value
            break
    return values


def load_state(state_path: Path) -> dict[str, object] | None:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state_path: Path, pid: int, python_path: Path, log_path: Path) -> None:
    payload = {
        "pid": pid,
        "python": str(python_path),
        "log_file": str(log_path),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return str(pid) in result.stdout


def stop_process_by_pid(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0 and is_process_running(pid):
            details = (result.stdout + "\n" + result.stderr).strip()
            raise RuntimeError(details or f"Не удалось остановить процесс PID {pid}.")
        return

    os.kill(pid, 15)


def ensure_env_file(repo_root: Path) -> Path:
    env_path = repo_root / ".env"
    example_path = repo_root / ".env.example"

    existing_values: dict[str, str] = {}
    existing_text = ""
    if env_path.exists():
        existing_text = env_path.read_text(encoding="utf-8")
        existing_values = parse_env_text(existing_text)

    should_overwrite = not env_path.exists()
    if env_path.exists():
        should_overwrite = ask_yes_no(
            "Файл .env уже существует. Перезаписать креды?", default=False
        )
        if not should_overwrite:
            try:
                validate_required_credentials(existing_values)
            except ValueError as exc:
                print(f"Текущий .env нельзя использовать: {exc}")
                should_overwrite = True

    if should_overwrite:
        if env_path.exists():
            base_text = existing_text
        elif example_path.exists():
            base_text = example_path.read_text(encoding="utf-8")
        else:
            base_text = ""

        credentials = prompt_required_credentials()
        validate_required_credentials(credentials)
        rendered = render_env_text(base_text, credentials)
        env_path.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"Файл {env_path.name} обновлён.")
    else:
        print("Использую текущий .env без изменений.")

    return env_path


def ensure_venv(repo_root: Path) -> Path:
    if os.name == "nt":
        venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = repo_root / ".venv" / "bin" / "python"

    if not venv_python.exists():
        print("Создаю виртуальное окружение .venv ...")
        subprocess.run(
            [sys.executable, "-m", "venv", str(repo_root / ".venv")],
            cwd=repo_root,
            check=True,
        )

    print("Устанавливаю зависимости из requirements.txt ...")
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=repo_root,
        check=True,
    )
    return venv_python


def stop_existing_instance_if_needed(repo_root: Path, state_path: Path) -> bool:
    state = load_state(state_path)
    if not state:
        return True

    pid = int(state.get("pid", 0))
    if not is_process_running(pid):
        state_path.unlink(missing_ok=True)
        return True

    should_restart = ask_yes_no(
        f"Бот уже запущен (PID {pid}). Перезапустить его?", default=False
    )
    if not should_restart:
        print("Оставляю текущий процесс без изменений.")
        return False

    stop_process_by_pid(pid)
    state_path.unlink(missing_ok=True)
    print(f"Процесс PID {pid} остановлен.")
    return True


def start_bot(repo_root: Path) -> int:
    state_path = repo_root / STATE_FILE_NAME
    if not stop_existing_instance_if_needed(repo_root, state_path):
        return 0

    ensure_env_file(repo_root)
    venv_python = ensure_venv(repo_root)

    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    console_log_path = logs_dir / CONSOLE_LOG_NAME

    creationflags = 0
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    with console_log_path.open("a", encoding="utf-8", newline="") as log_file:
        process = subprocess.Popen(
            [str(venv_python), "-m", "app.main"],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            **popen_kwargs,
        )

    time.sleep(2)
    return_code = process.poll()
    if return_code is not None:
        state_path.unlink(missing_ok=True)
        print(
            "Бот завершился сразу после запуска. "
            f"Проверьте логи: {console_log_path}"
        )
        return return_code

    save_state(state_path, process.pid, venv_python, console_log_path)
    print(f"Бот запущен в фоне. PID: {process.pid}")
    print(f"Логи процесса: {console_log_path}")
    print(f"Для остановки используйте: .\\stop_bot.ps1")
    return 0


def stop_bot(repo_root: Path) -> int:
    state_path = repo_root / STATE_FILE_NAME
    state = load_state(state_path)
    if not state:
        print("Файл состояния не найден. Похоже, бот не запущен этим скриптом.")
        return 0

    pid = int(state.get("pid", 0))
    if not is_process_running(pid):
        state_path.unlink(missing_ok=True)
        print(f"Процесс PID {pid} уже не запущен. Удаляю устаревший файл состояния.")
        return 0

    stop_process_by_pid(pid)
    state_path.unlink(missing_ok=True)
    print(f"Бот остановлен. PID: {pid}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Управление локальным запуском max2tg на Windows."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="записать креды при необходимости и запустить бота")
    subparsers.add_parser("stop", help="остановить бота, запущенного этим скриптом")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parent.parent

    if args.command == "start":
        return start_bot(repo_root)
    if args.command == "stop":
        return stop_bot(repo_root)

    parser.error(f"Неизвестная команда: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
