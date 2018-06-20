#
# Copyright (c) 2018 Hyeshik Chang
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import asyncio
import signal
import math
import sys
import os
from io import StringIO
from itertools import cycle
from concurrent.futures import (
    ProcessPoolExecutor, CancelledError, ThreadPoolExecutor)
from concurrent.futures.process import BrokenProcessPool
from . import *
from .io import (
    FASTQWriter, SequencingSummaryWriter, FinalSummaryTracker,
    create_adapter_dumps_inventory, create_events_inventory,
    link_fast5_files)
from .signal_analyzer import SignalAnalyzer
from .alignment_writer import AlignmentWriter
from .utils import *

FAST5_SUFFIX = '.fast5'


def force_terminate_executor(executor):
    executor._call_queue.empty()

    if executor._processes:
        alive_pids = set(
            pid for pid, proc in executor._processes.items()
            if proc.is_alive())
        for pid in alive_pids:
            os.kill(pid, signal.SIGKILL)


def scan_dir_recursive_worker(dirname, suffix=FAST5_SUFFIX):
    files, dirs = [], []
    for entryname in os.listdir(dirname):
        if entryname.startswith('.'):
            continue

        fullpath = os.path.join(dirname, entryname)
        if os.path.isdir(fullpath):
            dirs.append(entryname)
        elif entryname.lower().endswith(suffix):
            files.append(entryname)

    return dirs, files


def show_memory_usage():
    usages = open('/proc/self/statm').read().split()
    print('{:05d} MEMORY total={} RSS={} shared={} data={}'.format(
            batchid, usages[0], usages[1], usages[2], usages[4]))

def process_file(inputfile, analyzer):
    result = analyzer.process(inputfile)
    return result

def process_batch(batchid, reads, config):
    try:
        inputdir = config['inputdir']
        with SignalAnalyzer(config, batchid) as analyzer:
            results = []
            for f5file in reads:
                if os.path.exists(os.path.join(inputdir, f5file)):
                    results.append(process_file(f5file, analyzer))
                else:
                    results.append({'filename': f5file,
                                    'status': 'disappeared'})

            if config['barcoding']:
                barcode_assg = analyzer.predict_barcode_labels()
                for res in results:
                    if res['read_id'] in barcode_assg:
                        res['label'] = barcode_assg[res['read_id']]

            return results
    except Exception as exc:
        import traceback
        errorf = StringIO()
        traceback.print_exc(file=errorf)
        return (-1, 'Unhandled exception {}: {}'.format(type(exc).__name__, str(exc)),
                errorf.getvalue())

class ProcessingSession:

    def __init__(self, config, logger):
        self.running = True
        self.scan_finished = False
        self.reads_queued = self.reads_found = 0
        self.reads_processed = 0
        self.next_batch_id = 0
        self.files_done = set()
        self.active_batches = 0
        self.jobstack = []
        if config['live']:
            self.forced_flush_counter = 0

        self.config = config
        self.logger = logger

        self.executor_compute = ProcessPoolExecutor(config['parallel'])
        self.executor_io = ThreadPoolExecutor(2)
        self.executor_mon = ThreadPoolExecutor(2)

        self.loop = self.fastq_writer = self.alignment_writer = None
        self.pbar = None

    def __enter__(self):
        self.loop = asyncio.get_event_loop()
        self.executor_compute.__enter__()
        self.executor_io.__enter__()
        self.executor_mon.__enter__()

        for signame in 'SIGINT SIGTERM'.split():
            self.loop.add_signal_handler(getattr(signal, signame),
                                         self.stop, signame)

        if self.config['fastq_output']:
            self.fastq_writer = FASTQWriter(self.config['outputdir'],
                                            self.config['output_names'])
        self.seqsummary_writer = SequencingSummaryWriter(self.config['outputdir'],
                                                         self.config['output_names'])
        self.finalsummary_tracker = FinalSummaryTracker(self.config['output_names'])

        if self.config['minimap2_index']:
            self.show_message('==> Loading a minimap2 index file')
            self.alignment_writer = AlignmentWriter(
                self.config['minimap2_index'],
                os.path.join(self.config['outputdir'], 'bam', '{}.bam'),
                self.config['output_names'])

        return self

    def __exit__(self, *args):
        if self.fastq_writer is not None:
            self.fastq_writer.close()
            self.fastq_writer = None

        if self.seqsummary_writer is not None:
            self.seqsummary_writer.close()
            self.seqsummary_writer = None

        if self.alignment_writer is not None:
            self.alignment_writer.close()
            self.alignment_writer = None

        self.executor_mon.__exit__(*args)
        self.executor_io.__exit__(*args)
        self.executor_compute.__exit__(*args)
        self.loop.close()

    def errx(self, message):
        if self.running:
            errprint(message)
            self.stop('ERROR')

    def show_message(self, message):
        if not self.config['quiet']:
            print(message)

    def stop(self, signalname='unknown'):
        if self.running:
            errprint("\nTermination in process. Please wait for a moment.")
            self.running = False
        for task in asyncio.Task.all_tasks():
            task.cancel()

        self.loop.stop()

    def run_in_executor_compute(self, *args):
        return self.loop.run_in_executor(self.executor_compute, *args)

    def run_in_executor_io(self, *args):
        return self.loop.run_in_executor(self.executor_io, *args)

    def run_in_executor_mon(self, *args):
        return self.loop.run_in_executor(self.executor_mon, *args)

    async def run_process_batch(self, batchid, files):
        # Wait until the input files become ready if needed
        if self.config['analysis_start_delay'] > 0:
            try:
                await asyncio.sleep(self.config['analysis_start_delay'])
            except CancelledError:
                return

        self.active_batches += 1
        try:

            results = await self.run_in_executor_compute(
                process_batch, batchid, files, self.config)

            if len(results) > 0 and results[0] == -1: # Unhandled exception occurred
                error_message = results[1]
                self.logger.error(error_message)
                for line in results[2].splitlines():
                    self.logger.error(line)
                self.errx("ERROR: " + error_message)
                return

            # Remove duplicated results that could be fed multiple times in live monitoring
            nd_results = []
            for result in results:
                if result['filename'] not in self.files_done:
                    if result['status'] == 'okay':
                        self.files_done.add(result['filename'])
                    nd_results.append(result)
                else: # Cancel the duplicated result
                    self.reads_queued -= 1
                    self.reads_found -= 1

            if nd_results:
                if self.config['fastq_output']:
                    await self.run_in_executor_io(self.fastq_writer.write_sequences, nd_results)

                if self.config['fast5_output']:
                    await self.run_in_executor_io(link_fast5_files, nd_results)

                if self.config['minimap2_index']:
                    await self.run_in_executor_io(self.alignment_writer.process, nd_results)

                await self.run_in_executor_io(self.seqsummary_writer.write_results, nd_results)

                self.finalsummary_tracker.feed_results(nd_results)

        except (CancelledError, BrokenProcessPool):
            return
        except Exception as exc:
            self.logger.error('Unhandled error during processing reads', exc_info=exc)
            return self.errx('ERROR: Unhandled error ' + str(exc))
        finally:
            self.active_batches -= 1

        self.reads_processed += len(nd_results)
        self.reads_queued -= len(nd_results)

    def queue_processing(self, filepath):
        self.jobstack.append(filepath)
        self.reads_queued += 1
        self.reads_found += 1
        if len(self.jobstack) >= self.config['batch_chunk_size']:
            self.flush_jobstack()

    def flush_jobstack(self):
        if self.running and self.jobstack:
            batch_id = self.next_batch_id
            self.next_batch_id += 1

            # Remove files already processed successfully. The same file can be
            # fed into the stack while making transition from the existing
            # files to newly updated files from the live monitoring.
            files_to_submit = [
                filename for filename in self.jobstack
                if filename not in self.files_done]
            num_canceled = len(self.jobstack) - len(files_to_submit)
            if num_canceled:
                self.reads_queued -= num_canceled
                self.reads_found -= num_canceled
            del self.jobstack[:]

            if files_to_submit:
                work = self.run_process_batch(batch_id, files_to_submit)
                self.loop.create_task(work)

    async def scan_dir_recursive(self, topdir, dirname=''):
        if not self.running:
            return

        is_topdir = (dirname == '')

        try:
            errormsg = None
            dirs, files = await self.run_in_executor_mon(
                scan_dir_recursive_worker, os.path.join(topdir, dirname))
        except CancelledError as exc:
            if is_topdir: return
            else: raise exc
        except Exception as exc:
            errormsg = str(exc)

        if errormsg is not None:
            return self.errx('ERROR: ' + str(errormsg))

        for filename in files:
            filepath = os.path.join(dirname, filename)
            self.queue_processing(filepath)

        try:
            for subdir in dirs:
                subdirpath = os.path.join(dirname, subdir)
                await self.scan_dir_recursive(topdir, subdirpath)
        except CancelledError as exc:
            if is_topdir: return
            else: raise exc

        if is_topdir:
            self.flush_jobstack()
            self.scan_finished = True

    async def live_monitor_inputs(self, topdir, suffix=FAST5_SUFFIX):
        from inotify.adapters import InotifyTree
        from inotify.constants import IN_CLOSE_WRITE, IN_MOVED_TO

        watch_flags = IN_CLOSE_WRITE | IN_MOVED_TO
        topdir = os.path.abspath(topdir + '/') + '/' # add / for commonprefix
        try:
            evgen = InotifyTree(topdir, mask=watch_flags).event_gen()
            while True:
                event = await self.run_in_executor_mon(next, evgen)
                if event is None:
                    continue

                header, type_names, path, filename = event
                if 'IN_ISDIR' in type_names:
                    continue
                if header.mask & watch_flags and filename.lower().endswith(suffix):
                    common = os.path.commonprefix([topdir, path])
                    if common != topdir:
                        errprint("ERROR: Change of {} detected, which is outside "
                                 "{}.".format(path, topdir))
                        continue
                    relpath = os.path.join(path[len(common):], filename)
                    if relpath not in self.files_done:
                        self.forced_flush_counter = 0
                        self.queue_processing(relpath)

        except CancelledError:
            pass

    async def monitor_progresses(self):
        showprogress = not self.config['quiet']

        if showprogress:
            from progressbar import ProgressBar, widgets

            barformat_notfinalized = [
                widgets.AnimatedMarker(), ' ', widgets.Counter(), ' ',
                widgets.BouncingBar(), ' ', widgets.Timer()]

            class LooseAdaptiveETA(widgets.AdaptiveETA):
                # Stabilize the ETA on results from large batches rushes in
                NUM_SAMPLES = 100

            barformat_finalized = [
                widgets.AnimatedMarker(), ' ', widgets.Percentage(), ' of ',
                widgets.FormatLabel('%(max)d'), ' ', widgets.Bar(), ' ',
                widgets.Timer('Elapsed: %s'), ' ', LooseAdaptiveETA()]

            self.pbar = ProgressBar(widgets=barformat_notfinalized)
            self.pbar.start()
            notfinalized = True

        while self.running:
            if showprogress:
                if notfinalized and self.scan_finished:
                    notfinalized = False
                    self.pbar = ProgressBar(maxval=self.reads_found,
                                            widgets=barformat_finalized)
                    self.pbar.currval = self.reads_processed
                    self.pbar.start()
                else:
                    self.pbar.maxval = self.reads_found
                    self.pbar.update(self.reads_processed)

            try:
                await asyncio.sleep(0.3)
            except CancelledError:
                break

            if self.scan_finished and self.reads_queued <= 0:
                break

    async def monitor_progresses_live(self):
        self.show_message('==> Entering LIVE mode.')
        self.show_message('\nPress Ctrl-C when the sequencing run is finished.')
        prev_processed = prev_queued = prev_found = -1
        prev_message_width = 0
        iterglider = cycle(r'/-\|')

        # Estimate rough interval to force queue flushing on stagnated queueing.
        monitor_heartbeat = 0.3
        forcedflush = int(math.ceil(self.config['analysis_start_delay']
                                    / monitor_heartbeat))
        # Avoid flushing queue too often
        forcedflush = max(10 // monitor_heartbeat, forcedflush)

        while self.running:
            changedany = (
                prev_processed != self.reads_processed or
                prev_queued != self.reads_queued or
                prev_found != self.reads_found)

            if changedany or self.active_batches > 0:
                msg = "\rLIVE [{}] {} processed, {} queued ({} total reads)".format(
                        next(iterglider), self.reads_processed, self.reads_queued,
                        self.reads_found)
                if len(msg) < prev_message_width:
                    msg += ' ' * (prev_message_width - len(msg))

                sys.stdout.write(msg)
                sys.stdout.flush()

                prev_message_width = len(msg)
                prev_processed = self.reads_processed
                prev_queued = self.reads_queued
                prev_found = self.reads_found

            if self.reads_queued > 0:
                self.forced_flush_counter += 1

            try:
                await asyncio.sleep(monitor_heartbeat)
            except CancelledError:
                break

            if self.reads_queued > 0 and self.forced_flush_counter > forcedflush:
                self.forced_flush_counter = 0
                self.flush_jobstack()

    def terminate_executors(self):
        force_terminate_executor(self.executor_compute)

    def finalize_results(self):
        if self.config['dump_adapter_signals']:
            self.show_message("==> Creating an inventory for adapter signal dumps")
            adapter_dump_prefix = os.path.join(self.config['outputdir'], 'adapter-dumps')
            create_adapter_dumps_inventory(
                os.path.join(adapter_dump_prefix, 'inventory.h5'),
                os.path.join(adapter_dump_prefix, 'part-*.h5'))

        if self.config['dump_basecalls']:
            self.show_message("==> Creating an inventory for basecalled events")
            events_prefix = os.path.join(self.config['outputdir'], 'events')
            create_events_inventory(
                os.path.join(events_prefix, 'inventory.h5'),
                os.path.join(events_prefix, 'part-*.h5'))

    @classmethod
    def run(kls, config, logging):
        with kls(config, logging) as sess:
            sess.show_message("==> Processing FAST5 files")

            # Progress monitoring is not required during quiet AND live mode.
            if not config['live'] or not config['quiet']:
                monitor_func = (sess.monitor_progresses_live if config['live']
                                else sess.monitor_progresses)
                monitor_task = sess.loop.create_task(monitor_func())

            scanjob = sess.scan_dir_recursive(config['inputdir'])
            sess.loop.create_task(scanjob)

            if config['live']:
                livemon = sess.live_monitor_inputs(config['inputdir'])
                sess.loop.create_task(livemon)

            try:
                sess.loop.run_until_complete(monitor_task)
            except CancelledError:
                errprint('\nInterrupted')
            except Exception as exc:
                if (isinstance(exc, RuntimeError) and
                        exc.args[0].startswith('Event loop stopped before Future')):
                    pass
                else:
                    import traceback
                    errf = StringIO()
                    traceback.print_exc(file=errf)
                    errprint('\nERROR: ' + str(exc))
                    for line in errf.getvalue().splitlines():
                        logging.error(line)

            sess.terminate_executors()

            for task in asyncio.Task.all_tasks():
                if not (task.done() or task.cancelled()):
                    try:
                        sess.loop.run_until_complete(task)
                    except CancelledError:
                        errprint('\nInterrupted')
                    except Exception as exc:
                        errprint('\nERROR: ' + str(exc))

            if not config['quiet'] and sess.scan_finished:
                if sess.pbar is not None:
                    sess.pbar.finish()

                sess.show_message('\n')

                if sess.reads_found == sess.reads_processed:
                    sess.finalize_results()
                    sess.show_message('==> Finished.')
                    return sess.finalsummary_tracker.print_results
                else:
                    sess.show_message('==> Terminated.')
