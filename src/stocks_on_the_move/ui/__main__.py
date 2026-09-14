"""Allow ``python -m stocks_on_the_move.ui`` as an alternative to the console script."""

from stocks_on_the_move.ui.app import main

if __name__ == "__main__":
    main()
