import asyncio
import logging
import msvcrt
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO, AsyncIterator, Tuple

import aiohttp
import discord
import win32file
from aiohttp_requests import requests
from dataclasses_json import dataclass_json
from dateutil.parser import parse


@dataclass_json
@dataclass
class Config:
    player_name: str
    eft_install_dir: str
    webhook_url: str


config: Config
logger: logging.Logger


def setup_logging(*, debug_on_stdout=False) -> None:
    formatter = logging.Formatter(
        "[%(asctime)s][%(levelname)s][%(name)s][%(funcName)s] %(message)s")

    stdout_level = logging.DEBUG if debug_on_stdout else logging.INFO
    log_stdout = logging.StreamHandler(stream=sys.stdout)
    log_stdout.setLevel(stdout_level)
    log_stdout.setFormatter(formatter)

    log_stderr = logging.StreamHandler(stream=sys.stderr)
    log_stderr.setLevel(logging.WARNING)
    log_stderr.setFormatter(formatter)

    # filter discord and websocket on stdout
    def filter_discord(record):
        return not (record.name.startswith("discord.")
                    or record.name.startswith("websockets."))

    # filter WARNING and above on stdout
    def filter_above_info(record):
        return record.levelno <= logging.INFO

    log_stdout.addFilter(filter_discord)
    log_stderr.addFilter(filter_discord)
    log_stdout.addFilter(filter_above_info)

    logging.basicConfig(level=logging.NOTSET,
                        handlers=[log_stdout, log_stderr])

    global logger
    logger = logging.getLogger(__name__)


def get_newest_log_filename() -> str:
    # get newest log directory
    newest_entry = None
    newest_time = -1
    for entry in os.scandir(f"{config.eft_install_dir}\\Logs\\"):
        entry: os.DirEntry
        cur_time = entry.stat().st_ctime
        if cur_time > newest_time:
            newest_time = cur_time
            newest_entry = entry

    assert newest_entry is not None, "no log directories, start EFT before starting this script"

    # get date prefix of the log filename
    _, _, log_filename_prefix = newest_entry.name.partition("log_")

    return f"{config.eft_install_dir}\\Logs\\{newest_entry.name}\\{log_filename_prefix} application.log"


def open_log_file() -> Tuple[TextIO, str]:
    log_filename = get_newest_log_filename()

    logger.debug(f"Opening log file {log_filename}")
    # source:
    # https://www.thepythoncorner.com/2016/10/python-how-to-open-a-file-on-windows-without-locking-it/
    # get a handle using win32 API, specifying SHARED access!
    handle = win32file.CreateFile(log_filename,
                                  win32file.GENERIC_READ,
                                  win32file.FILE_SHARE_DELETE |
                                  win32file.FILE_SHARE_READ |
                                  win32file.FILE_SHARE_WRITE,
                                  None,
                                  win32file.OPEN_EXISTING,
                                  0,
                                  None)
    # detach the handle
    detached_handle = handle.Detach()
    # get a file descriptor associated to the handle
    file_descriptor = msvcrt.open_osfhandle(
        detached_handle, os.O_RDONLY)
    # open the file descriptor
    f = open(file_descriptor, encoding="UTF-8")
    # seek to end
    # f.seek(0, os.SEEK_END)

    logger.debug(f"Opened log file {log_filename}")
    return f, log_filename


async def log_follow() -> AsyncIterator[Tuple[str, bool]]:
    f, f_name = open_log_file()
    no_new_lines_counter = 0

    # only post live sessions to Discord, post previous sessions to console only
    scanned_through_file = False

    # read indefinitely
    while True:
        # read until end of file
        while True:
            try:
                line = f.readline()
            except UnicodeDecodeError:
                sys.stderr.write(
                    "[WARN] Skipped line because of decode error\n")
                line = "DECODE_ERROR"
                logger.debug(f"DECODE_ERROR")
            # check if we're at EOF
            if not line:
                scanned_through_file = True
                no_new_lines_counter += 1
                break
            else:
                no_new_lines_counter = 0
            logger.debug(f"[READ]{line}")
            yield line, scanned_through_file

        # if we've seen no new lines for 30 iterations, check if there is a new log file
        if no_new_lines_counter > 30:
            no_new_lines_counter = 0
            f_new, f_new_name = open_log_file()
            # If it's a different file, replace old handle. Otherwise, discard new handle.
            if f_new_name != f_name:
                logger.debug("new log file")
                f.close()
                f = f_new
                f_name = f_new_name
                scanned_through_file = False
            else:
                logger.debug("no new log file")
                f_new.close()

        logger.debug(f"GOING_TO_SLEEP")
        await asyncio.sleep(1)
        logger.debug(f"WOKE_UP")


async def parse_line(line, is_live_session):
    match = re.search(r"^([^|]*).*Status: Busy, Ip: ([^,]+).*shortId: (....)", line)
    if match is None:
        return
    time_str, ip, lobby_id = match.group(1, 2, 3)

    time = parse(time_str)

    response = await requests.get(f"http://ip-api.com/json/{ip}")
    response_json = await response.json()

    if not response_json["status"] == "success":
        logger.error(f"unsuccessfull ip location query: {response_json}")
        return

    await post_location(lobby_id, response_json["country"], time, is_live_session)


async def post_location(lobby_id: str, country: str, time: datetime, is_live_session: bool) -> None:
    if not is_live_session:
        logger.info(f"Previous session @ {time.strftime('%H:%M:%S')}: lobby_id {lobby_id}, country {country}")

    if not is_live_session:
        return
    logger.info(f"Live session: lobby_id {lobby_id}, country {country}")

    embed = discord.Embed(title=config.player_name)
    embed.add_field(name="Lobby ID", value=lobby_id)
    embed.add_field(name="Country", value=country)

    # Send message via webhook
    async with aiohttp.ClientSession() as session:
        webhook = discord.Webhook.from_url(config.webhook_url,
                                           adapter=discord.AsyncWebhookAdapter(session))
        await webhook.send(embed=embed)


async def main() -> None:
    async for line, is_live_session in log_follow():
        await parse_line(line, is_live_session)


if __name__ == "__main__":
    setup_logging(debug_on_stdout=False)
    logger = logging.getLogger(__name__)
    logger.info("Reading config...")

    if not os.path.isfile("config.json"):
        logger.error("No config found. Make sure you have a config.json next to this script. Press any key to exit...")
        input()
        sys.exit(1)

    with open("config.json", "r") as config_file:
        config = Config.from_json(config_file.read())

    logger.info("Started. Keep this script open.")
    asyncio.run(main())
