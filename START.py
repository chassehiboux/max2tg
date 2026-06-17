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
    "TG_ADMIN_ID",
)

STATE_FILE_NAME = ".max2tg-runner.json"
CONSOLE_LOG_NAME = "max2tg-console.log"
STARTUP_MAX_RETRIES = 3
STARTUP_RETRY_DELAY_SEC = 3
STARTUP_POLL_INTERVAL_SEC = 0.5
TELEGRAM_STARTUP_TIMEOUT_SEC = 20
MAX_STARTUP_TIMEOUT_SEC = 30
MAX_AUTH_STALL_SEC = 10
TELEGRAM_READY_MARKER = "Telegram polling started"
MAX_READY_MARKER = "Authorized! my_id="
MAX_AUTH_MARKER = "Handshake OK → sending auth token..."
MAX_AUTH_TIMEOUT_MARKER = "Max authorization timed out"
MAX_AUTH_FAILED_MARKER = "Max authorization failed. Check MAX_TOKEN and MAX_DEVICE_ID."
TELEGRAM_RETRYABLE_MARKERS = (
    "telegram.error.TimedOut",
    "httpx.ConnectTimeout",
    "httpcore.ConnectTimeout",
    "httpx.ReadTimeout",
    "httpcore.ReadTimeout",
)


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

    tg_admin_id = values["TG_ADMIN_ID"]
    try:
        int(tg_admin_id)
    except ValueError as exc:
        raise ValueError("TG_ADMIN_ID должен быть целым числом.") from exc


def normalize_command(raw: str | None) -> str | None:
    if raw is None:
        return None
    return {
        "1": "start",
        "start": "start",
        "2": "stop",
        "stop": "stop",
    }.get(raw.strip().lower())


def prompt_two_choice(
    prompt: str,
    first_label: str,
    second_label: str,
    default_choice: int,
) -> int:
    if default_choice not in {1, 2}:
        raise ValueError("default_choice must be 1 or 2")

    while True:
        print(prompt)
        print(f"1. {first_label}")
        print(f"2. {second_label}")
        answer = input(f"Введите 1 или 2 [Enter = {default_choice}]: ").strip()
        if not answer:
            return default_choice
        if answer in {"1", "2"}:
            return int(answer)
        print("Некорректный выбор. Введите 1 или 2.")


def prompt_command_choice() -> str:
    choice = prompt_two_choice(
        "Выберите режим запуска:",
        "Запустить бота",
        "Остановить бота",
        default_choice=1,
    )
    return "start" if choice == 1 else "stop"


def prompt_required_credentials(existing_values: dict[str, str] | None = None) -> dict[str, str]:
    prompts = (
        ("MAX_TOKEN", "MAX_TOKEN (__oneme_auth из web.max.ru)"),
        ("MAX_DEVICE_ID", "MAX_DEVICE_ID (__oneme_device_id из web.max.ru)"),
        ("TG_BOT_TOKEN", "TG_BOT_TOKEN (токен от @BotFather)"),
        ("TG_ADMIN_ID", "TG_ADMIN_ID (ваш user id в Telegram)"),
    )

    print("Введите обязательные креды для Max и Telegram.")
    existing_values = existing_values or {}
    values: dict[str, str] = {}
    for key, label in prompts:
        current_value = existing_values.get(key, "").strip()
        prompt_label = label
        if current_value:
            prompt_label = f"{label} [Enter = оставить текущее значение]"
        while True:
            value = input(f"{prompt_label}: ").strip()
            if not value and current_value:
                value = current_value
            if not value:
                print("Значение не может быть пустым.")
                continue
            if key == "TG_ADMIN_ID":
                try:
                    int(value)
                except ValueError:
                    print("TG_ADMIN_ID должен быть целым числом.")
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


def read_log_chunk(log_path: Path, offset: int) -> tuple[str, int]:
    if not log_path.exists():
        return "", offset

    file_size = log_path.stat().st_size
    if file_size < offset:
        offset = 0

    with log_path.open("rb") as stream:
        stream.seek(offset)
        chunk = stream.read()

    return chunk.decode("utf-8", errors="replace"), file_size


def is_retryable_telegram_startup_error(log_text: str) -> bool:
    return any(marker in log_text for marker in TELEGRAM_RETRYABLE_MARKERS)


def is_retryable_max_startup_error(log_text: str) -> bool:
    return MAX_AUTH_TIMEOUT_MARKER in log_text


def is_max_credentials_error(log_text: str) -> bool:
    return MAX_AUTH_FAILED_MARKER in log_text


def build_telegram_connectivity_hint(env_values: dict[str, str]) -> str:
    tg_proxy = (env_values.get("TG_PROXY") or "").strip()
    if tg_proxy:
        return (
            "После 3 ретраев бот так и не подключился к Telegram.\n"
            "В .env сейчас указан TG_PROXY.\n"
            "Попробуйте сменить прокси или временно убрать TG_PROXY из .env."
        )

    return (
        "После 3 ретраев бот так и не подключился к Telegram.\n"
        "В .env TG_PROXY не указан.\n"
        "Попробуйте прописать рабочий прокси в TG_PROXY или настроить проксирование другим способом."
    )


def build_max_connectivity_hint() -> str:
    return (
        "После 3 ретраев бот так и не подключился к MAX.\n"
        "Проверьте, как у вас настроена маршрутизация доменов: подключение к MAX должно идти из России."
    )


def build_max_credentials_hint() -> str:
    return "MAX отверг авторизацию. Проверьте значения MAX_TOKEN и MAX_DEVICE_ID в .env."


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        stop_process_by_pid(process.pid)
    except Exception:
        pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


def monitor_startup(process: subprocess.Popen, log_path: Path, start_offset: int) -> tuple[str, int, str]:
    started_at = time.monotonic()
    offset = start_offset
    collected_log = ""
    telegram_ready = False
    max_ready = False
    max_auth_started_at: float | None = None

    while True:
        new_chunk, offset = read_log_chunk(log_path, offset)
        if new_chunk:
            collected_log += new_chunk
            if TELEGRAM_READY_MARKER in collected_log:
                telegram_ready = True
            if MAX_READY_MARKER in collected_log:
                max_ready = True
            if MAX_AUTH_MARKER in collected_log and max_auth_started_at is None:
                max_auth_started_at = time.monotonic()
            if is_max_credentials_error(collected_log):
                terminate_process(process)
                return "max_credentials_error", process.poll() or 1, collected_log
            if is_retryable_max_startup_error(collected_log):
                terminate_process(process)
                return "max_error", process.poll() or 1, collected_log

        return_code = process.poll()
        if return_code is not None:
            if is_retryable_telegram_startup_error(collected_log):
                return "telegram_error", return_code, collected_log
            if is_max_credentials_error(collected_log):
                return "max_credentials_error", return_code, collected_log
            if is_retryable_max_startup_error(collected_log):
                return "max_error", return_code, collected_log
            if telegram_ready and not max_ready:
                return "max_error", return_code, collected_log
            return "process_exit", return_code, collected_log

        if telegram_ready and max_ready:
            return "started", 0, collected_log

        now = time.monotonic()
        if not telegram_ready and now - started_at >= TELEGRAM_STARTUP_TIMEOUT_SEC:
            terminate_process(process)
            return "telegram_error", process.poll() or 1, collected_log

        if telegram_ready:
            if max_auth_started_at is not None and not max_ready and now - max_auth_started_at >= MAX_AUTH_STALL_SEC:
                terminate_process(process)
                return "max_error", process.poll() or 1, collected_log
            if not max_ready and now - started_at >= MAX_STARTUP_TIMEOUT_SEC:
                terminate_process(process)
                return "max_error", process.poll() or 1, collected_log

        time.sleep(STARTUP_POLL_INTERVAL_SEC)


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
        should_overwrite = (
            prompt_two_choice(
                "Файл .env уже существует. Что сделать?",
                "Перезаписать креды",
                "Оставить текущий .env",
                default_choice=2,
            )
            == 1
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

        credentials = prompt_required_credentials(existing_values if env_path.exists() else None)
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

    should_restart = (
        prompt_two_choice(
            f"Бот уже запущен (PID {pid}). Что сделать?",
            "Перезапустить бота",
            "Оставить текущий процесс",
            default_choice=2,
        )
        == 1
    )
    if not should_restart:
        print("Оставляю текущий процесс без изменений.")
        return False

    stop_process_by_pid(pid)
    state_path.unlink(missing_ok=True)
    print(f"Процесс PID {pid} остановлен.")
    return True


def spawn_bot_process(repo_root: Path, venv_python: Path, console_log_path: Path) -> subprocess.Popen:
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
        return subprocess.Popen(
            [str(venv_python), "-m", "app.main"],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            **popen_kwargs,
        )


def start_bot(repo_root: Path) -> int:
    state_path = repo_root / STATE_FILE_NAME
    if not stop_existing_instance_if_needed(repo_root, state_path):
        return 0

    env_path = ensure_env_file(repo_root)
    env_values = parse_env_text(env_path.read_text(encoding="utf-8"))
    venv_python = ensure_venv(repo_root)

    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    console_log_path = logs_dir / CONSOLE_LOG_NAME

    total_attempts = STARTUP_MAX_RETRIES + 1
    for attempt_index in range(total_attempts):
        attempt_number = attempt_index + 1
        print(f"Запускаю бота: попытка {attempt_number}/{total_attempts} ...")
        start_offset = console_log_path.stat().st_size if console_log_path.exists() else 0
        process = spawn_bot_process(repo_root, venv_python, console_log_path)
        status, return_code, _ = monitor_startup(process, console_log_path, start_offset)

        if status == "started":
            save_state(state_path, process.pid, venv_python, console_log_path)
            print(f"Бот запущен в фоне. PID: {process.pid}")
            print(f"Логи процесса: {console_log_path}")
            print("Для остановки используйте: .\\scripts\\stop_bot.ps1")
            return 0

        state_path.unlink(missing_ok=True)

        if status == "max_credentials_error":
            print(build_max_credentials_hint())
            print(f"Проверьте логи: {console_log_path}")
            return return_code or 1

        if attempt_index < STARTUP_MAX_RETRIES and status in {"telegram_error", "max_error"}:
            if status == "telegram_error":
                print(
                    f"Не удалось подключиться к Telegram при старте. "
                    f"Делаю ретрай {attempt_index + 1}/{STARTUP_MAX_RETRIES} ..."
                )
            else:
                print(
                    f"Не удалось подключиться к MAX при старте. "
                    f"Делаю ретрай {attempt_index + 1}/{STARTUP_MAX_RETRIES} ..."
                )
            time.sleep(STARTUP_RETRY_DELAY_SEC)
            continue

        if status == "telegram_error":
            print(build_telegram_connectivity_hint(env_values))
            print(f"Проверьте логи: {console_log_path}")
            return return_code or 1

        if status == "max_error":
            print(build_max_connectivity_hint())
            print(f"Проверьте логи: {console_log_path}")
            return return_code or 1

        print(
            "Бот завершился сразу после запуска. "
            f"Проверьте логи: {console_log_path}"
        )
        return return_code or 1

    print("Запуск прерван.")
    return 1


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
    parser.add_argument(
        "command",
        nargs="?",
        help="1/start - запустить, 2/stop - остановить",
    )
    return parser


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    if len(sys.argv) == 1:
        command = prompt_command_choice()
    else:
        parser = build_parser()
        args = parser.parse_args()
        command = normalize_command(args.command)
        if command is None:
            parser.error("Используйте 1/start для запуска или 2/stop для остановки.")

    if command == "start":
        return start_bot(repo_root)
    if command == "stop":
        return stop_bot(repo_root)

    raise ValueError(f"Неизвестная команда: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
