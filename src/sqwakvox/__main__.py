import logging

from sqwakvox.app import SqwakvoxApp


def main() -> None:
    logging.basicConfig(
        filename="sqwakvox.log",
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.info("Sqwakvox application starting...")
    app = SqwakvoxApp()
    app.run()


if __name__ == "__main__":
    main()
