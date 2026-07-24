"""
Scheduler - Runs scraper every 1 minute
"""
import time
import schedule
from scraper import FammaScraper
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scraper = FammaScraper()

def job():
    logger.info("Running scheduled scrape...")
    scraper.run_scrape()

schedule.every(1).minutes.do(job)

if __name__ == "__main__":
    job()
    logger.info("Scheduler started - scraping every 1 minute")
    while True:
        schedule.run_pending()
        time.sleep(1)
