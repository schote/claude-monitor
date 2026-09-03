# Copy to config.py, fill in, and upload alongside main.py:
#   mpremote connect /dev/cu.usbmodem<X> fs cp config.py :config.py
# config.py is gitignored so credentials stay out of the repo.

WIFI_SSID = "my-network"
WIFI_PASSWORD = "my-password"

# The daemon on the Raspberry Pi. Use the *IP address*, not a name: the
# board would otherwise have to ask the router's DNS on every fetch, and a
# router keeps serving a stale record for a good while after a machine moves
# from Wi-Fi to a cable - which is exactly how "no daemon" appears while the
# daemon is running perfectly. Give the Pi a DHCP reservation and put that
# address here.
USAGE_URL = "http://192.168.1.50:8000/usage"

# Tried in order, only after USAGE_URL has failed twice in a row. Worth
# listing a second address the Pi answers on, or its name as a last resort
# in case the address it was given ever changes.
USAGE_URLS = (
    "http://raspberrypi:8000/usage",
)

FETCH_INTERVAL_S = 180

# Optional - the defaults in main.py are fine for normal use.
# REBOOT_AFTER_S = 15 * 60   # reboot if nothing has been fetched for this long
# WATCHDOG = True            # reboot if the UI thread itself stops running
# WIFI_POWER_SAVE = False    # True saves power, at the cost of missed packets
