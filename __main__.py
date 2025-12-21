"""Entry point for python -m logmon"""
try:
    from .main import main
except ImportError:
    from main import main

if __name__ == "__main__":
    main()
