import atexit
import logging

from sqwakvox.app import SqwakvoxApp
from sqwakvox.telemetry import setup_telemetry, shutdown_telemetry


def _setup_chat_logging() -> None:
    chat_handler = logging.FileHandler("sqwakvoxchat.log", mode="a")
    chat_handler.setLevel(logging.DEBUG)
    chat_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    chat_handler.setFormatter(chat_formatter)

    chat_logger = logging.getLogger("sqwakvox.chat")
    chat_logger.setLevel(logging.DEBUG)
    chat_logger.addHandler(chat_handler)
    chat_logger.propagate = False

    for lg_name in [
        "langchain",
        "langchain_core",
        "langchain_community",
        "langchain_openai",
        "langchain_anthropic",
        "langchain_mistralai",
        "langchain_google_genai",
    ]:
        lg = logging.getLogger(lg_name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(chat_handler)
        lg.propagate = False

    logging.getLogger("any_agent").setLevel(logging.DEBUG)
    logging.getLogger("any_agent").addHandler(chat_handler)
    logging.getLogger("any_agent").propagate = False


def main() -> None:
    logging.basicConfig(
        filename="sqwakvox.log",
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _setup_chat_logging()
    setup_telemetry()
    atexit.register(shutdown_telemetry)
    logging.info("Sqwakvox application starting...")
    from sqwakvox.presenter import Presenter

    presenter = Presenter()
    app = SqwakvoxApp(presenter=presenter)
    app.run()
    # Clean up the presenter's polling tasks on shutdown.
    import asyncio

    asyncio.get_event_loop().run_until_complete(presenter.close())


if __name__ == "__main__":
    main()
