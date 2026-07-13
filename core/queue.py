import threading


class ConversionQueue:
    def __init__(self):
        self._queue = []
        self._completed = []
        self._lock = threading.Lock()
        self.running = False
        self.paused = False
        self.current_job = None
        self._worker_thread = None
        self.on_job_update = None
        self.on_queue_finished = None

    def add(self, job):
        with self._lock:
            job.status = "queued"
            self._queue.append(job)
        self._notify_job_update(job, "queued")

    def add_batch(self, jobs):
        with self._lock:
            for job in jobs:
                job.status = "queued"
                self._queue.append(job)
        for job in jobs:
            self._notify_job_update(job, "queued")

    def remove(self, job):
        with self._lock:
            if job in self._queue:
                self._queue.remove(job)
            elif job in self._completed:
                self._completed.remove(job)

    def remove_many(self, jobs):
        with self._lock:
            for job in jobs:
                if job in self._queue:
                    self._queue.remove(job)
                elif job in self._completed:
                    self._completed.remove(job)

    def clear(self):
        self.stop()
        with self._lock:
            self._queue.clear()
            self._completed.clear()

    def requeue(self, job):
        with self._lock:
            if job in self._completed:
                self._completed.remove(job)
            if job not in self._queue:
                job.status = "queued"
                job.progress = 0.0
                job.error = None
                job.output_path = None
                self._queue.append(job)
            self._notify_job_update(job, "queued")

    def get_queue(self):
        with self._lock:
            return list(self._queue)

    def get_all_items(self):
        with self._lock:
            items = []
            if self.current_job and self.current_job not in items:
                items.append(self.current_job)
            items.extend(self._queue)
            items.extend(self._completed)
        return items

    @property
    def queue_size(self):
        with self._lock:
            return len(self._queue)

    @property
    def is_idle(self):
        with self._lock:
            return (not self.running and len(self._queue) == 0
                    and self.current_job is None)

    def start(self):
        if self.running:
            return
        self.running = True
        self.paused = False
        self._worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self._worker_thread.start()

    def stop(self):
        self.running = False
        self.paused = False
        if self.current_job:
            self.current_job.cancel()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def _process_queue(self):
        while self.running:
            if self.paused:
                threading.Event().wait(0.1)
                continue

            with self._lock:
                if self._queue:
                    self.current_job = self._queue.pop(0)
                else:
                    self.current_job = None
                    self.running = False
                    if self.on_queue_finished:
                        self.on_queue_finished()
                    break

            if self.current_job:
                self.current_job.run(
                    progress_callback=self._on_progress,
                    status_callback=self._on_status
                )
                # After running, move to completed list
                with self._lock:
                    if self.current_job.status in ("completed", "error", "cancelled"):
                        if self.current_job not in self._completed:
                            self._completed.append(self.current_job)

        with self._lock:
            self.current_job = None
            self.running = False

    def _on_progress(self, job):
        self._notify_job_update(job, "progress")

    def _on_status(self, job, status):
        self._notify_job_update(job, status)

    def _notify_job_update(self, job, status):
        if self.on_job_update:
            self.on_job_update(job, status)
