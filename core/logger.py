import os
import logging
from datetime import datetime


LOG_DIR = os.path.join(os.path.expanduser("~"), ".prorez", "logs")


def get_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("prorez")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    log_file = os.path.join(LOG_DIR, "prorez.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    session_log = os.path.join(
        LOG_DIR, f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    sh = logging.FileHandler(session_log, encoding="utf-8")
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def log_job_start(job):
    logger = get_logger()
    cmd_str = " ".join(job.build_command())
    logger.info("=" * 60)
    logger.info(f"START  | {job.filename}")
    logger.info(f"INPUT  | {job.filepath}")
    logger.info(f"OUTPUT | {job.output_path}")
    logger.info(f"CODEC  | source={job.video_codec} target={job.settings.get('encoder','?')}")
    logger.info(f"CMD    | {cmd_str}")
    logger.info("-" * 60)


def log_job_progress(job):
    logger = get_logger()
    logger.debug(
        f"PROG   | {job.filename} | {job.progress:.1f}% | "
        f"fps={job.current_fps:.1f} speed={job.speed:.2f}x"
    )


def log_job_done(job):
    logger = get_logger()
    logger.info(f"DONE   | {job.filename} | output={job.output_path}")
    if job.output_path and os.path.exists(job.output_path):
        out_size = os.path.getsize(job.output_path)
        logger.info(f"SIZE   | {job.filename} | {out_size / 1_000_000:.1f} MB")


def log_job_error(job):
    logger = get_logger()
    logger.error(f"FAIL   | {job.filename}")
    logger.error(f"ERROR  | {job.error}")
    logger.info("=" * 60)


def log_job_cancelled(job):
    logger = get_logger()
    logger.warning(f"CANCEL | {job.filename}")


def log_session_summary(jobs):
    logger = get_logger()
    total = len(jobs)
    ok = sum(1 for j in jobs if j.status == "completed")
    fail = sum(1 for j in jobs if j.status == "error")
    cancel = sum(1 for j in jobs if j.status == "cancelled")
    logger.info("=" * 60)
    logger.info(f"SESSION SUMMARY | total={total} done={ok} failed={fail} cancelled={cancel}")
    logger.info("=" * 60)


def log_file_probe(filepath, info):
    logger = get_logger()
    logger.info("-" * 60)
    logger.info(f"PROBE  | {os.path.basename(filepath)}")
    logger.info(f"PATH   | {filepath}")
    logger.info(f"SIZE   | {info.get('size', 0) / 1_000_000:.1f} MB")
    logger.info(f"DUR    | {info.get('duration', 0):.1f}s")
    logger.info(f"BR     | {int(info.get('bitrate', 0)) / 1000:.0f} kbps")

    for i, stream in enumerate(info.get("streams", [])):
        stype = stream.get("type", "?")
        codec = stream.get("codec", "?")
        codec_long = stream.get("codec_long", "")

        if stype == "video":
            w = stream.get("width", 0)
            h = stream.get("height", 0)
            fps = stream.get("fps", 0)
            pix = stream.get("pix_fmt", "")
            sbr = int(stream.get("bitrate", 0)) // 1000 if stream.get("bitrate") else 0
            logger.info(
                f"STR#V{i} | {codec} ({codec_long}) | {w}x{h} @ {fps}fps | "
                f"{pix} | {sbr}kbps"
            )
        elif stype == "audio":
            ch = stream.get("channels", "?")
            sr = stream.get("sample_rate", "?")
            abr = int(stream.get("bitrate", 0)) // 1000 if stream.get("bitrate") else 0
            logger.info(
                f"STR#A{i} | {codec} ({codec_long}) | {ch}ch @ {sr}Hz | "
                f"{abr}kbps"
            )
        else:
            logger.info(f"STR#{i}  | {stype} | {codec} ({codec_long})")

    logger.info("-" * 60)
