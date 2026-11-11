"""PyInstaller 打包入口。"""
import multiprocessing
import sys

from pp6kit.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
