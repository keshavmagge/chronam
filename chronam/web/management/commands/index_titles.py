import logging

from django.core.management.base import BaseCommand
    
from chronam.utils import configure_logging
from chronam.web.index import index_titles

configure_logging("index_titles_logging.config", "index_titles.log")

_logger = logging.getLogger(__name__)

class Command(BaseCommand):

    def handle(self, **options):
        _logger.info("indexing titles")
        index_titles()
        _logger.info("finished indexing titles")
