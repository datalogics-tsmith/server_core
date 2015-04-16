from nose.tools import set_trace
import datetime
import time
import traceback
from sqlalchemy.sql.functions import func

from model import (
    get_one_or_create,
    Identifier,
    LicensePool,
    Timestamp,
    Work,
)

class Monitor(object):

    ONE_MINUTE_AGO = datetime.timedelta(seconds=60)

    def __init__(
            self, _db, name, interval_seconds=1*60,
            default_start_time=None):
        self._db = _db
        self.service_name = name
        self.interval_seconds = interval_seconds
        self.stop_running = False
        if not default_start_time:
             default_start_time = (
                 datetime.datetime.utcnow() - self.ONE_MINUTE_AGO)
        self.default_start_time = default_start_time

    def run(self):        
        self.timestamp, new = get_one_or_create(
            self._db, Timestamp,
            service=self.service_name,
            create_method_kwargs=dict(
                timestamp=self.default_start_time
            )
        )
        start = self.timestamp.timestamp or self.default_start_time

        while not self.stop_running:
            cutoff = datetime.datetime.utcnow()
            new_timestamp = self.run_once(start, cutoff) or cutoff
            duration = datetime.datetime.utcnow() - cutoff
            to_sleep = self.interval_seconds-duration.seconds-1
            self.cleanup()
            self.timestamp.timestamp = new_timestamp
            self._db.commit()
            if to_sleep > 0:
                print "Sleeping for %.1f" % to_sleep
                time.sleep(to_sleep)
            start = new_timestamp

    def run_once(self, start, cutoff):
        raise NotImplementedError()

    def cleanup(self):
        pass


class IdentifierSweepMonitor(Monitor):

    def __init__(self, _db, name, interval_seconds=3600,
                 default_counter=0, batch_size=100):
        super(IdentifierSweepMonitor, self).__init__(
            _db, name, interval_seconds)
        self.default_counter = default_counter
        self.batch_size = batch_size

    def run(self):        
        self.timestamp, new = get_one_or_create(
            self._db, Timestamp,
            service=self.service_name,
            create_method_kwargs=dict(
                counter=self.default_counter
            )
        )
        offset = self.timestamp.counter or self.default_counter

        started_at = datetime.datetime.utcnow()
        while not self.stop_running:
            print "Old offset: %s" % offset
            new_offset = self.run_once(offset)
            to_sleep = 0
            if new_offset == 0:
                # We completed a sweep. Sleep until the next sweep
                # begins.
                duration = datetime.datetime.now() - started_at
                to_sleep = self.interval_seconds - duration.seconds
                self.cleanup()
            self.counter = new_offset
            self.timestamp.counter = self.counter
            self._db.commit()
            print "New offset: %s" % new_offset
            if to_sleep > 0:
                print "Sleeping for %.1f" % to_sleep
                time.sleep(to_sleep)
            offset = new_offset

    def run_once(self, offset):
        q = self.identifier_query().filter(
            Identifier.id > offset).order_by(
            Identifier.id).limit(self.batch_size)
        identifiers = q.all()
        if identifiers:
            self.process_batch(identifiers)
            return identifiers[-1].id
        else:
            return 0

    def identifier_query(self):
        return self._db.query(Identifier)

    def process_batch(self, identifiers):
        raise NotImplementedError()


class PresentationReadyMonitor(Monitor):
    """A monitor that makes works presentation ready.

    By default this works by passing the work's active edition into
    ensure_coverage() for each of a list of CoverageProviders. If all
    the ensure_coverage() calls succeed, presentation of the work is
    calculated and the work is marked presentation ready.
    """
    def __init__(self, _db, coverage_providers,
                 calculate_work_even_if_no_author=False):
        super(PresentationReadyMonitor, self).__init__(
            _db, "Make Works Presentation Ready")
        self.coverage_providers = coverage_providers
        self.calculate_work_even_if_no_author = calculate_work_even_if_no_author

    def run_once(self, start, cutoff):
        # Consolidate works.
        LicensePool.consolidate_works(
            self._db,
            calculate_work_even_if_no_author=self.calculate_work_even_if_no_author)

        unready_works = self._db.query(Work).filter(
            Work.presentation_ready==False).filter(
                Work.presentation_ready_exception==None).order_by(
                func.random()).limit(10)
        # Work in batches of 10 works. This lets us consolidate and
        # parallelize IO-bound activities like uploading assets to S3.
        keep_going = True
        while keep_going and unready_works.count():
            keep_going = self.make_batch_presentation_ready(
                unready_works.all())
        if not keep_going:
            print "[PRESENTATION READY] An entire batch failed. Giving up for now."

    def make_batch_presentation_ready(self, batch):
        one_success = False
        for work in batch:
            failures = None
            exception = None
            try:
                failures = self.prepare(work)
            except Exception, e:
                tb = traceback.format_exc()
                print "[PRESENTATION READY MONITOR] Caught exception %s" % tb
                failures = True
            if failures and failures not in (None, True):
                if isinstance(failures, list):
                    # This is a list of providers that failed.
                    if len(failures):
                        provider_names = ", ".join(
                            [x.service_name for x in failures])
                        exception = "Provider(s) failed: %s" % provider_names
                    else:
                        # Just kidding, the list is empty, there were
                        # no failures.
                        pass
                else:
                    exception = str(failures)
            if exception:
                work.presentation_ready_exception = exception
            else:
                work.calculate_presentation(choose_edition=False)
                work.set_presentation_ready()                    
                one_success = True
        self.finalize_batch()
        return one_success

    def prepare(self, work):
        edition = work.primary_edition
        identifier = edition.primary_identifier
        overall_success = True
        failures = []
        for provider in self.coverage_providers:
            if edition.data_source in provider.input_sources:
                coverage_record = provider.ensure_coverage(edition)
                if not coverage_record:
                    failures.append(provider)
        return failures

    def finalize_batch(self):
        self._db.commit()
