import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from plumbum import local, BG
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler
from docker_support import DockerSignalMonitor

logger = logging.getLogger('ocrmypdf-auto')

INPUT_BASE = local.path(os.getenv('OCR_INPUT_DIR', '/input'))
OUTPUT_BASE = local.path(os.getenv('OCR_OUTPUT_DIR', '/output'))
OCRMYPDF = local['ocrmypdf']

class OcrmypdfConfig(object):

    def __init__(self, input_path, output_path, config_file=None):
        self.logger = logger.getChild('config')
        self.input_path = local.path(input_path)
        self.output_path = local.path(output_path)
        self.options = {}
        self.set_default_options()
        self.parse_config_file(config_file)

    def set_default_options(self):
        def looks_like_yes(string):
            return string is not None and string.lower() in ['1', 'y', 'yes', 'on', 't', 'true']

        langs = os.getenv('OCR_LANGUAGES')
        if langs is not None:
            self.options['--language'] = langs

        sidecar = os.getenv('OCR_GENERATE_SIDECAR', '0')
        if looks_like_yes(sidecar):
            self.options['--sidecar'] = self.output_path.with_suffix('.txt')

        jobs = os.getenv('OCR_CPUS_PER_JOB', '1')
        self.options['--jobs'] = jobs

        rotate = os.getenv('OCR_ROTATE', '0')
        if looks_like_yes(rotate):
            self.options['--rotate-pages'] = None

            rotate_confidence = os.getenv('OCR_ROTATE_CONFIDENCE')
            rotate_confidence = try_float(rotate_confidence, None)
            if rotate_confidence is not None:
                self.options['--rotate-pages-threshold'] = rotate_confidence

        deskew = os.getenv('OCR_DESKEW', '0')
        if looks_like_yes(deskew):
            self.options['--deskew'] = None

        clean = os.getenv('OCR_CLEAN', '0')
        if looks_like_yes(clean):
            self.options['--clean'] = None
        elif clean.lower() in ('final'):
            self.options['--clean-final'] = None

        skip_text = os.getenv('OCR_SKIP_TEXT', '0')
        if looks_like_yes(skip_text):
            self.options['--skip-text'] = None

        #TODO: Config path for user words & patterns

    def parse_config_file(self, config_file):
        pass

    def get_ocrmypdf_arguments(self):
        args = []
        for arg, val in self.options.items():
            args.append(arg)
            if val is not None:
                args.append(val)
        args.append(self.input_path)
        args.append(self.output_path)
        return args

def try_float(string, default_value):
    try:
        return float(string)
    except (ValueError, TypeError):
        return default_value

class OcrTask(object):

    NEW = 'NEW'
    QUEUED = 'QUEUED'
    SLEEPING = 'SLEEPING'
    ACTIVE = 'ACTIVE'
    DONE = 'DONE'

    COALESCING_DELAY = timedelta(seconds=try_float(os.getenv('OCR_PROCESSING_DELAY'), 3.0))

    def __init__(self, path, submit_func, done_callback):
        self.logger = logger.getChild('task')
        self.path = path
        self.submit_func = submit_func
        self.done_callback = done_callback
        self.last_touch = None
        self.future = None
        self.state = OcrTask.NEW
        self.touch()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return "<OcrTask [{}] {} last_touch={} future={}>".format(
                self.state,
                self.path,
                self.last_touch,
                self.future)

    def enqueue(self):
        assert self.state in [OcrTask.NEW, OcrTask.ACTIVE]
        self.state = OcrTask.QUEUED
        self.logger.debug('OcrTask Enqueued: [%s] %s', self.state, self.path)
        self.future = self.submit_func(self.process)

    def touch(self):
        self.logger.debug('OcrTask Touched: [%s] %s', self.state, self.path)
        self.last_touch = datetime.now()
        if self.state == OcrTask.NEW:
            self.enqueue()

    def cancel(self):
        self.logger.debug('OcrTask Canceled: [%s] %s', self.state, self.path)
        self.last_touch = None
        if self.state == OcrTask.QUEUED:
            self.future.cancel()
            self.done()

    def done(self):
        self.logger.debug('OcrTask Done: [%s] %s', self.state, self.path)
        self.state = OcrTask.DONE
        if self.done_callback:
            self.done_callback(self)

    def process(self, skip_delay=False):
        """ocrmypdf processing task"""
        self.logger.warn('Processing: %s', self.path)
        start_ts = datetime.now()

        # Coalescing sleep as long as file is still being modified
        while not skip_delay:
            # Check for cancellation
            if self.last_touch is None:
                self.logger.info('Processing canceled: %s', self.path)
                self.done()
                return

            due = self.last_touch + OcrTask.COALESCING_DELAY
            wait_span = due - datetime.now()
            if wait_span <= timedelta(0):
                break;
            self.logger.debug('Sleeping for %f: [%s] %s', wait_span.total_seconds(), self.state, self.path)
            self.state = OcrTask.SLEEPING
            time.sleep(wait_span.total_seconds())

        # Actually run ocrmypdf
        self.state = OcrTask.ACTIVE
        self.last_touch = None
        self.logger.info('Running ocrmypdf: %s', self.path)

        # TODO: Take output path from ctor
        out_path = OUTPUT_BASE / (self.path - INPUT_BASE)
        # TODO: Take config from ctor
        config = OcrmypdfConfig(self.path, out_path)
        ocrmypdf = OCRMYPDF.__getitem__(config.get_ocrmypdf_arguments())
        (rc, stdout, stderr) = ocrmypdf.run()
        self.logger.debug('ocrmypdf returns %d with stdout [%s] and stderr[%s]', rc, stdout, stderr)

        runtime = datetime.now() - start_ts
        self.logger.warn('Processing complete in %f seconds: %s', round(runtime.total_seconds(), 2), self.path)

        # Check for modification during sleep
        if self.last_touch is not None:
            self.logger.info('Modified during ocrmypdf execution: [%s] %s', self.state, self.path)
            self.enqueue()
            return

        # We're done
        self.done()


class AutoOcrWatchdogHandler(PatternMatchingEventHandler):
    """
    Matches files to process through ocrmypdf and kicks off processing
    """

    def __init__(self, file_touched_callback, file_deleted_callback):
        super(AutoOcrWatchdogHandler, self).__init__(patterns = ["*.pdf"], ignore_directories=True)
        self.logger = logger.getChild('watchdog_handler')
        self.file_touched_callback = file_touched_callback
        self.file_deleted_callback = file_deleted_callback

    def touch_file(self, path):
        if self.file_touched_callback:
            self.file_touched_callback(path)

    def delete_file(self, path):
        if self.file_deleted_callback:
            self.file_deleted_callback(path)

    def on_moved(self, event):
        self.logger.debug("File moved: %s -> %s", event.src_path, event.dest_path)
        self.delete_file(local.path(event.src_path))
        self.touch_file(local.path(event.dest_path))

    def on_created(self, event):
        self.logger.debug("File created: %s", event.src_path)
        self.touch_file(local.path(event.src_path))

    def on_deleted(self, event):
        self.logger.debug("File deleted: %s", event.src_path)
        self.delete_file(local.path(event.src_path))

    def on_modified(self, event):
        self.logger.debug("File modified: %s", event.src_path)
        self.touch_file(local.path(event.src_path))


class AutoOcrScheduler(object):

    def __init__(self):
        self.logger = logger.getChild('scheduler')

        self.input_path = INPUT_BASE
        self.current_tasks = {}

        # Create a Threadpool to run OCR tasks on
        self.threadpool = ThreadPoolExecutor(max_workers=3)

        # Wire up an AutoOcrWatchdogHandler
        watchdog_handler = AutoOcrWatchdogHandler(self.on_file_touched, self.on_file_deleted)

        # Schedule watchdog to observe the input path
        self.observer = Observer()
        self.observer.schedule(watchdog_handler, INPUT_BASE, recursive=True)
        self.observer.start()
        self.logger.warn('Watching %s', self.input_path)

    def shutdown(self):
        # Shut down the feed of incoming watchdog events
        if self.observer:
            self.logger.debug('Shutting down filesystem watchdog...')
            self.observer.unschedule_all()
            self.observer.stop()

        # Cancel all outstanding cancelable tasks
        self.logger.debug('Canceling all %d in-flight tasks...', len(self.current_tasks))
        tasks = [task for _, task in self.current_tasks.items()]
        for task in tasks:
            task.cancel()

        # Wait for the threadpool to clean up
        if self.threadpool:
            self.logger.debug('Shutting down threadpool...')
            self.threadpool.shutdown()
            self.threadpool = None

        # Wait for the watchdog to clean up
        if self.observer:
            self.logger.debug('Cleaning up filesystem watchdog...')
            self.observer.join()
            self.observer = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()
        return False

    def on_file_touched(self, path):
        if path in self.current_tasks:
            self.current_tasks[path].touch()
        else:
            self.current_tasks[path] = OcrTask(path, self.threadpool.submit, self.on_task_done)

    def on_file_deleted(self, path):
        if path in self.current_tasks:
            self.current_tasks[path].cancel()

    def on_task_done(self, task):
        del self.current_tasks[task.path]


if __name__ == "__main__":
    logging_level = os.getenv('OCR_VERBOSITY')
    if logging_level is None:
        logging_level = logging.WARNING
    elif getattr(logging, logging_level.upper(), None) is not None:
        logging_level = getattr(logging, logging_level.upper())
    else:
        logging_level = int(try_float(logging_level, logging.WARNING))

    if logging_level < logging.WARNING:
        logging.basicConfig(level=logging_level,
                            format='%(asctime)s [%(threadName)s] - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    else:
        logging.basicConfig(level=logging_level,
                            format='%(asctime)s - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

    docker_monitor = DockerSignalMonitor()

    # Run an AutoOcrScheduler until terminated
    with AutoOcrScheduler() as scheduler:
        # Wait in the main thread to be killed
        signum = docker_monitor.wait_for_exit()
        logger.warn('Signal %d (%s) Received. Shutting down...', signum, DockerSignalMonitor.SIGNUMS_TO_NAMES[signum])

