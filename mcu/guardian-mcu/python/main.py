# The Linux side of Guardian runs as its own service (board/run.sh), not inside this App.
# This App only exists to build and flash the STM32 sketch; its Python part just idles.
import time

from arduino.app_utils import App


def loop():
    time.sleep(60)


App.run(user_loop=loop)
