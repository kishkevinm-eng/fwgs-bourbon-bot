
"""FWGS Whiskey Release Monitor with automatic watchdog recovery."""

import logging
import os
import signal
import sys
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .config import Config, Constants, setup_logging
from .scraper import ProductScraper
from .notifier import DiscordNotifier
from .storage import ProductStorage


logger = logging.getLogger(__name__)

# Global monitoring state
running = True
last_successful_check = time.monotonic()

# Watchdog settings
WATCHDOG_TIMEOUT = 600  # 10 minutes
WATCHDOG_INTERVAL = 30  # Check watchdog every 30 seconds


def watchdog_loop():
    """
    Automatically terminate the process when monitoring stalls.

    Railway should restart the bot when the restart
    policy is set to On Failure.
    """
    global running
    global last_successful_check

    logger.info(
        "Watchdog active: timeout=%s seconds",
        WATCHDOG_TIMEOUT
    )

    while running:
        time.sleep(WATCHDOG_INTERVAL)

        if not running:
            break

        elapsed = time.monotonic() - last_successful_check

        if elapsed > WATCHDOG_TIMEOUT:
            logger.critical(
                "WATCHDOG TRIGGERED: No successful FWGS check "
                "for %.0f seconds. Exiting for Railway restart.",
                elapsed
            )

            # Force-exit even if Playwright is stuck.
            # Railway must have On Failure restart enabled.
            os._exit(1)


def signal_handler(signum, frame):
    """Handle graceful shutdown signals."""
    global running

    logger.info("Shutdown signal received, stopping...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def run_check(
    storage: ProductStorage,
    notifier: DiscordNotifier,
    is_first_run: bool = False
) -> bool:
    """
    Execute one monitoring check.

    Returns True only when scraping and saving succeed.
    """
    global last_successful_check

    logger.info("=" * 60)
    logger.info(
        "BOURBON MONITOR - %s",
        datetime.now().strftime("%B %d, %Y at %I:%M %p")
    )
    logger.info("=" * 60)

    try:
        # Create scraper
        scraper = ProductScraper(
            Config.TARGET_URL,
            headless=Config.HEADLESS
        )

        # Load previously saved products
        old_products = storage.load()

        # Run synchronous Playwright in its own thread
        with ThreadPoolExecutor(max_workers=1) as executor:
            new_products = executor.submit(
                scraper.scrape
            ).result()

        # Never overwrite history with an empty scrape
        if not new_products:
            logger.error(
                "Scrape returned zero products. "
                "Skipping this check."
            )
            return False

        # Protect against unexpectedly large product drops
        if old_products:
            drop_percentage = (
                len(old_products) - len(new_products)
            ) / len(old_products)

            if drop_percentage >= Constants.PRODUCT_DROP_THRESHOLD:
                logger.error(
                    "Product count dropped from %s to %s "
                    "(%.0f%%). Skipping check.",
                    len(old_products),
                    len(new_products),
                    drop_percentage * 100
                )
                return False

        # Identify newly listed products
        new_arrivals = storage.get_new_products(
            old_products,
            new_products
        )

        # Identify products becoming available
        now_available = []

        if old_products and not is_first_run:
            old_by_name = {
                p["name"].lower(): p
                for p in old_products
            }

            for product in new_products:
                name = product["name"].lower()
                old_product = old_by_name.get(name)

                if not old_product:
                    continue

                old_status = old_product.get(
                    "status", "available"
                )

                new_status = product.get(
                    "status", "available"
                )

                if (
                    old_status in ("coming_soon", "lottery")
                    and new_status == "available"
                ):
                    now_available.append(product)

                    logger.info(
                        "STATUS CHANGE: %s is now available",
                        product["name"]
                    )

        # New-product notifications
        if new_arrivals and not is_first_run:
            notifier.send_new_products(new_arrivals)

            logger.info(
                "NEW ARRIVALS: %s products",
                len(new_arrivals)
            )

        elif new_arrivals and is_first_run:
            logger.info(
                "Establishing baseline with %s products",
                len(new_arrivals)
            )

        else:
            logger.info("No new products found")

        # Availability notifications
        if now_available:
            notifier.send_now_available(now_available)

            logger.info(
                "NOW AVAILABLE: %s products",
                len(now_available)
            )

        # Save product history
        if not storage.save(new_products):
            logger.error(
                "Could not save product history"
            )
            return False

        # Update watchdog only after a successful check
        last_successful_check = time.monotonic()

        logger.info(
            "Check complete: %s products tracked",
            len(new_products)
        )

        logger.info(
            "Watchdog timer reset successfully"
        )

        return True

    except KeyboardInterrupt:
        raise

    except Exception as e:
        logger.error(
            "Error during monitoring check: %s",
            e,
            exc_info=True
        )

        harmless_errors = [
            "browser has been closed",
            "target page",
            "context has been closed"
        ]

        error_text = str(e).lower()

        if not any(
            error in error_text
            for error in harmless_errors
        ):
            try:
                notifier.send_error(str(e))
            except Exception as notify_error:
                logger.error(
                    "Failed to send Discord error: %s",
                    notify_error
                )

        # Do not reset watchdog after failure
        return False


def send_startup_notification(storage, notifier):
    """
    Send startup notification with a 30-minute cooldown.
    Store the timestamp beside the product-history file.
    """
    try:
        startup_file = (
            Path(Config.PRODUCTS_FILE).parent
            / ".last_startup"
        )

        startup_file.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        should_notify = True

        if startup_file.exists():
            elapsed = time.time() - startup_file.stat().st_mtime

            if elapsed < 1800:
                should_notify = False

                logger.info(
                    "Skipping startup notification "
                    "(30-minute cooldown)"
                )

        if should_notify:
            current_products = storage.load()
            notifier.send_startup(current_products)

        startup_file.write_text(str(time.time()))

    except Exception as e:
        logger.warning(
            "Could not send startup notification: %s",
            e
        )


def main():
    """Main monitoring loop with automatic watchdog."""
    global running
    global last_successful_check

    setup_logging()

    logger.info("=" * 60)
    logger.info("BOURBON ONLINE EXCLUSIVES MONITOR")
    logger.info("=" * 60)
    logger.info("Target URL: %s", Config.TARGET_URL)
    logger.info(
        "Check Interval: %s minutes",
        Config.CHECK_INTERVAL
    )
    logger.info("Headless Mode: %s", Config.HEADLESS)
    logger.info("Log Directory: %s", Config.LOG_DIR)
    logger.info("Data File: %s", Config.PRODUCTS_FILE)
    logger.info(
        "Watchdog Timeout: %s seconds",
        WATCHDOG_TIMEOUT
    )
    logger.info("=" * 60)

    # Initialize storage and Discord
    storage = ProductStorage(Config.PRODUCTS_FILE)
    notifier = DiscordNotifier(Config.DISCORD_WEBHOOK_URL)

    # Start the watchdog before the initial scrape
    last_successful_check = time.monotonic()

    watchdog = threading.Thread(
        target=watchdog_loop,
        name="fwgs-watchdog",
        daemon=True
    )

    watchdog.start()

    logger.info("Automatic watchdog started")

    # Establish baseline only if no saved products exist
    existing_products = storage.load()
    is_first_run = not bool(existing_products)

    logger.info("Running initial monitoring check...")

    initial_success = run_check(
        storage,
        notifier,
        is_first_run=is_first_run
    )

    if initial_success:
        send_startup_notification(storage, notifier)
    else:
        logger.warning(
            "Initial check failed; startup notification skipped"
        )

    # Continuous monitoring
    while running:
        try:
            wait_seconds = Config.CHECK_INTERVAL * 60

            logger.info(
                "Next check in %s minutes...",
                Config.CHECK_INTERVAL
            )

            # Responsive shutdown
            for _ in range(int(wait_seconds)):
                if not running:
                    break

                time.sleep(1)

            if not running:
                break

            # Run next monitoring check
            success = run_check(
                storage,
                notifier,
                is_first_run=False
            )

            if not success:
                logger.warning(
                    "Monitoring check failed. "
                    "Watchdog timer not reset."
                )

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            break

        except Exception as e:
            logger.error(
                "Unexpected main loop error: %s",
                e,
                exc_info=True
            )

            time.sleep(60)

    logger.info("Bourbon monitor stopped")


if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        logger.critical(
            "Fatal monitoring error: %s",
            e,
            exc_info=True
        )

        sys.exit(1)
