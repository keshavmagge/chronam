import os
import re
import logging

from time import time
from datetime import datetime

from lxml import etree
from solr import SolrConnection

from django.db import reset_queries

import j2k

from chronam import utils
from chronam.web import models
from chronam.web.models import Batch, Issue, Title, Awardee, Page, OCR
from chronam.web.models import LoadBatchEvent

from chronam.settings import STORAGE
from chronam.settings import SOLR

# some xml namespaces used in batch metadata
ns = {
    'ndnp'  : 'http://www.loc.gov/ndnp',
    'mods'  : 'http://www.loc.gov/mods/v3',
    'mets'  : 'http://www.loc.gov/METS/',
    'np'    : 'urn:library-of-congress:ndnp:mets:newspaper',
    'xlink' : 'http://www.w3.org/1999/xlink',
    'mix'   : 'http://www.loc.gov/mix/',
    'xhtml' : 'http://www.w3.org/1999/xhtml'
}

_logger = logging.getLogger(__name__)

class BatchLoader(object):
    """This class allows you to load a batch into the database. A loader 
    object serves as a context for a particular batch loading job.
    """

    def __init__(self, storage=STORAGE, process_ocr=True):
        """Create a loader for a given storage area on the filesystem 
        The storage area is where batch files are located. By default
        the loader will use the STORAGE location in the site-wide settings
        file.

        The process_ocr parameter is used (mainly in testing) when we don't 
        want to spend time actually extracting ocr text and indexing.
        """
        self.storage = os.path.abspath(storage)
        self.issues_processed = 0
        self.pages_processed = 0
        self.PROCESS_OCR = process_ocr
        if self.PROCESS_OCR: 
            self.solr = SolrConnection(SOLR)

    def _find_batch_file(self, batch):
        """
        TODO: Who can we toss the requirement at to make this
        available in a canonical location?
        """
        # look for batch_1.xml, BATCH_1.xml, etc
        for alias in ["batch_1.xml", "BATCH_1.xml", "batchfile_1.xml", "batch_2.xml", "BATCH_2.xml", "batch.xml"]:
            # TODO: might we want 'batch.xml' first? Leaving last for now to
            # minimize impact.
            if os.path.exists(os.path.join(batch.path, alias)):
                validated_batch_file = alias
                break
            validated_batch_file = 'batch.xml'
        else:
            raise BatchLoaderException(
                "could not find batch_1.xml (or any of its aliases) in %s--has the batch been validated?" % batch.path)
        return validated_batch_file
        
    def _sanity_check_batch(self, batch):
        if not os.path.exists(batch.path):
            raise BatchLoaderException("batch does not exist at %s" % batch.path)
        batch.validated_batch_file = self._find_batch_file(batch)

    def load_batch(self, batch_name, strict=True):
        """Load a batch, and return a Batch instance for the batch
        that was loaded.

          loader.load_batch('batch_curiv_ahwahnee_ver01')

        """
        batch_name = _normalize_batch_name(batch_name)
        if not strict:
            try:
                batch = Batch.objects.get(name=batch_name)
                _logger.info("Batch already loaded: %s" % batch_name)
                return batch
            except Batch.DoesNotExist, e:
                pass

        _logger.info("loading batch: %s" % batch_name)
        t0 = time()
        times = []

        event = LoadBatchEvent(batch_name=batch_name, message="starting load")
        event.save()

        batch = None
        try:
            # build a Batch object for the batch location
            batch = self._get_batch(batch_name, create=True)
            self._sanity_check_batch(batch)

            # stash it away for processing later on
            self.current_batch = batch

            # parse the batch.xml and load up each issue mets file
            doc = etree.parse(batch.validated_batch_file_path)

            for e in doc.xpath('ndnp:reel', namespaces=ns):
                
                reel_number = e.attrib['reelNumber'].strip()
                
                try:
                    reel = models.Reel.objects.get(number=reel_number, 
                                                   batch=batch)
                except models.Reel.DoesNotExist, e:
                    reel = models.Reel(number=reel_number, batch=batch)
                    reel.save()

            for e in doc.xpath('ndnp:issue', namespaces=ns):
                mets_file = os.path.join(batch.path, e.text)
                mets_file = os.path.normpath(mets_file)
                try:
                    issue = self._load_issue(mets_file)
                except ValueError, e:
                    _logger.exception(e)
                    continue
                reset_queries()
                self.issues_processed += 1
                seconds = time() - t0
                times.append((seconds, self.pages_processed))

            # commit new changes to the solr index, if we are indexing
            if self.PROCESS_OCR:
                self.solr.commit()
            
            batch.save()
            msg = "processed %s issues" % self.issues_processed 
            event = LoadBatchEvent(batch_name=batch_name, message=msg)
            _logger.info(msg)
            event.save()

            _chart(times)
        except BaseException, e:
            msg = "unable to load batch: %s" % e
            _logger.error(msg)
            _logger.exception(e)
            event = LoadBatchEvent(batch_name=batch_name, message=msg)
            event.save()
            raise BatchLoaderException(msg)
        return batch

    def _get_batch(self, batch_name, create=False):
        if create:
            batch = self._create_batch(batch_name)
        else:
            batch = Batch.objects.get(name=batch_name)
        return batch
        
    def _create_batch(self, batch_name):
        if Batch.objects.filter(name=batch_name).count()!=0:
            raise BatchLoaderException("batch %s already loaded" % batch_name)
        batch = Batch()
        batch.name = os.path.basename(batch_name)
        try:
            _, org_code, name_part, version = batch_name.split("_", 3)
            awardee_org_code = org_code
            batch.awardee = Awardee.objects.get(org_code=awardee_org_code)
        except Awardee.DoesNotExist, e:
            msg = "no awardee for org code: %s" % awardee_org_code 
            _logger.error(msg)
            raise BatchLoaderException(msg)
        batch.save()
        return batch

    def _load_issue(self, mets_file):
        _logger.debug("parsing issue mets file: %s" % mets_file)
        doc = etree.parse(mets_file)

        # get the mods for the issue
        div = doc.xpath('.//mets:div[@TYPE="np:issue"]', namespaces=ns)[0]
        dmdid = div.attrib['DMDID']
        mods = dmd_mods(doc, dmdid)

        # set up a new Issue
        issue = Issue()
        issue.volume = mods.xpath(
            'string(.//mods:detail[@type="volume"]/mods:number[1])', 
            namespaces=ns).strip()
        issue.number = mods.xpath(
            'string(.//mods:detail[@type="issue"]/mods:number[1])', 
            namespaces=ns).strip()
        issue.edition = int(mods.xpath( 
                'string(.//mods:detail[@type="edition"]/mods:number[1])', 
                namespaces=ns))
        issue.edition_label = mods.xpath( 
                'string(.//mods:detail[@type="edition"]/mods:caption[1])', 
                namespaces=ns).strip()

        # parse issue date
        date_issued = mods.xpath('string(.//mods:dateIssued)', namespaces=ns)
        issue.date_issued = datetime.strptime(date_issued, '%Y-%m-%d')

        # attach the Issue to the appropriate Title
        lccn = mods.xpath('string(.//mods:identifier[@type="lccn"])', 
            namespaces=ns).strip()
        title = Title.objects.get(lccn=lccn)
        issue.title = title

        issue.batch = self.current_batch
        issue.save()
        _logger.debug("saved issue: %s" % issue.url)

        notes = [] 
        for mods_note in mods.xpath('.//mods:note', namespaces=ns): 
            type = mods_note.xpath('string(./@type)') 
            label = mods_note.xpath('string(./@displayLabel)') 
            text = mods_note.xpath('string(.)') 
            note = models.IssueNote(type=type, label=label, text=text) 
            notes.append(note) 
        issue.notes = notes 
        issue.save()

        # attach pages: lots of logging because it's expensive
        for page_div in div.xpath('.//mets:div[@TYPE="np:page"]', 
                                  namespaces=ns):

            try:
                page = self._load_page(doc, page_div, issue)
            except BatchLoaderException, e:
                _logger.exception(e)
                continue
            finally:
                self.pages_processed += 1

        return issue

    def _load_page(self, doc, div, issue):
        dmdid = div.attrib['DMDID']
        mods = dmd_mods(doc, dmdid)
        page = Page()
        
        seq_string = mods.xpath(
            'string(.//mods:extent/mods:start)', namespaces=ns)
        try:
            page.sequence = int(seq_string)
        except ValueError, e:
            raise BatchLoaderException("could not determine sequence number for page from '%s'" % seq_string)
        page.number = mods.xpath(
            'string(.//mods:detail[@type="page number"])', 
            namespaces=ns
            ).strip()

        reel_number = mods.xpath(
            'string(.//mods:identifier[@type="reel number"])', 
            namespaces=ns
            ).strip()
        try:
            reel = models.Reel.objects.get(number=reel_number, 
                                           batch=self.current_batch)
            page.reel = reel
        except models.Reel.DoesNotExist, e:
            if reel_number:
                reel = models.Reel(number=reel_number,
                                   batch=self.current_batch,
                                   implicit=True)
                reel.save()
                page.reel = reel
            else:
                _logger.warn("unable to find reel number in page metadata")

        _logger.info("Assigned page sequence: %s" % page.sequence)

        _section_dmdid = div.xpath(
            'string(ancestor::mets:div[@TYPE="np:section"]/@DMDID)',
            namespaces=ns)
        if _section_dmdid:
            section_mods = dmd_mods(doc, _section_dmdid)
            section_label = section_mods.xpath(
                'string(.//mods:detail[@type="section label"]/mods:number[1])', 
                namespaces=ns).strip()
            if section_label:
                page.section_label = section_label

        page.issue = issue

        _logger.info("Saving page. issue date: %s, page sequence: %s" % (issue.date_issued, page.sequence))

        # TODO - consider the possibility of executing the file name
        #        assignments (below) before this page.save().
        page.save()

        notes = []
        for mods_note in mods.xpath('.//mods:note', namespaces=ns):
            type = mods_note.xpath('string(./@type)')
            label = mods_note.xpath('string(./@displayLabel)')
            text = mods_note.xpath('string(.)').strip()
            note = models.PageNote(type=type, label=label, text=text)
            notes.append(note)
        page.notes = notes
            

        # there's a level indirection between the METS structmap and the
        # details about specific files in this package ...
        # so we have to first get the FILEID from the issue div in the 
        # structmap and then use it to look up the file details in the
        # larger document. 

        for fptr in div.xpath('./mets:fptr', namespaces=ns):
            file_id = fptr.attrib['FILEID']
            file_el = doc.xpath('.//mets:file[@ID="%s"]' % file_id, 
                namespaces=ns)[0]
            file_type = file_el.attrib['USE']

            # get the filename relative to the storage location
            file_name = file_el.xpath('string(./mets:FLocat/@xlink:href)', 
                namespaces=ns)
            file_name = os.path.join(os.path.dirname(doc.docinfo.URL),
                file_name)
            file_name = self.storage_relative_path(file_name)

            if file_type == 'master':
                page.tiff_filename = file_name
            elif file_type == 'service':
                page.jp2_filename = file_name
                try:
                    # extract image dimensions from technical metadata for jp2
                    for admid in file_el.attrib['ADMID'].split(' '):
                        length, width = get_dimensions(doc, admid)
                        if length and width:
                            page.jp2_width = width
                            page.jp2_length = length
                            break
                except KeyError, e:
                    _logger.info("Could not determine dimensions of jp2 for issue: %s page: %s... trying harder..." % (page.issue, page))
                    width, length = j2k.dimensions(page.jp2_abs_filename)
                    page.jp2_width = width
                    page.jp2_length = length
                    #raise BatchLoaderException("Could not determine dimensions of jp2 for issue: %s page: %s" % (page.issue, page))
                if not page.jp2_width:
                    raise BatchLoaderException("No jp2 width for issue: %s page: %s" % (page.issue, page))
                if not page.jp2_length:
                    raise BatchLoaderException("No jp2 length for issue: %s page: %s" % (page.issue, page))
            elif file_type == 'derivative':
                page.pdf_filename = file_name
            elif file_type == 'ocr':
                page.ocr_filename = file_name

        if page.ocr_filename:
            # don't incurr overhead of extracting ocr text, word coordinates
            # and indexing unless the batch loader has been set up to do it
            if self.PROCESS_OCR:
                self.process_ocr(page)
        else:
            _logger.info("No ocr filename for issue: %s page: %s" % (page.issue, page))

        _logger.debug("saving page: %s" % page.url)
        page.save()
        return page

    def process_ocr(self, page, index=True):
        _logger.debug("extracting ocr text and word coords for %s" %
            page.url)

        text, coords = utils.ocr_extractor(page.ocr_abs_filename)
        ocr = OCR()
        ocr.text, ocr.word_coordinates, ocr.page = text, coords, page
        ocr.save()
        page.ocr = ocr 
        if index:
            _logger.debug("indexing ocr for: %s" % page.url)
            self.solr.add(**page.solr_doc)
            page.indexed = True
        page.save()

    def storage_relative_path(self, path):
        """returns a relative path for a given file path within a batch, so 
        that storage can be re-homed without having to rewrite paths in the db
        """
        full_path =  os.path.abspath(path)
        rel_path = full_path.replace(self.current_batch.path, '')
        rel_path = rel_path.lstrip('./')
        return rel_path

    def purge_batch(self, batch_name):
        event = LoadBatchEvent(batch_name=batch_name, message="starting purge")
        event.save()

        try:
            batch = self._get_batch(batch_name)
            self._purge_batch(batch)
            event = LoadBatchEvent(batch_name=batch_name, message="purged")
            event.save()
        except Exception, e:
            msg = "purge failed: %s" % e
            _logger.error(msg)
            _logger.exception(e)
            event = LoadBatchEvent(batch_name=batch_name, message=msg)
            event.save()
            raise BatchLoaderException(msg)            

    def _purge_batch(self, batch):
        batch_name = batch.name
        # just delete batch causes memory to bloat out
        # so we do it piece-meal
        for issue in batch.issues.all():
            for page in issue.pages.all():
                page.delete()
                reset_queries()
            issue.delete()
        batch.delete()
        self.solr.delete_query('batch:"%s"' % batch_name)
        self.solr.commit()

class BatchLoaderException(RuntimeError):
    pass


def dmd_mods(doc, dmdid):
    """a helper that returns mods inside a dmdSec with a given ID
    """
    xpath ='.//mets:dmdSec[@ID="%s"]/descendant::mods:mods' % dmdid
    return doc.xpath(xpath, namespaces=ns)[0]

def get_dimensions(doc, admid):
    """return length, width for an image from techincal metadata with a given 
    admid
    """
    xpath = './/mets:techMD[@ID="%s"]/mets:mdWrap/mets:xmlData/mix:mix/mix:ImagingPerformanceAssessment/mix:SpatialMetrics/%s'
    length = doc.xpath(xpath % (admid, 'mix:ImageLength'), namespaces=ns)
    width = doc.xpath(xpath % (admid, 'mix:ImageWidth'), namespaces=ns)
    if length and width:
        return length[0].text, width[0].text
    return None, None

def _chart(times):
    """
    Creates a google chart given a list of times as floats.
    """
    num = len(times)
    if num == 0:
        return
    step = max(num/100, 1) # we only want around a 100 datapoints for our chart
    f_times = ["%.2f" % (times[i][0]) for i in range(0, num, step)]
    counts = ["%s" % (times[i][1]) for i in range(0, num, step)]
    _logger.info("\n    http://chart.apis.google.com/chart?cht=lxy&chs=200x125&chd=t:%s|%s&chds=%s,%s,%s,%s" % (",".join(f_times), ",".join(counts), f_times[0], f_times[-1], counts[0], counts[-1]))

def _normalize_batch_name(batch_name):
    batch_name = batch_name.rstrip('/')
    batch_name = os.path.basename(batch_name)
    if not re.match(r'batch_\w+_\w+_ver\d\d', batch_name):
        msg = 'unrecognized format for batch name %s' % batch_name
        _logger.error(msg)
        raise BatchLoaderException(msg)
    return batch_name
        