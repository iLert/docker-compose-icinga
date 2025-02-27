#!/usr/bin/env python


# iLert Icinga Plugin
#
# Copyright (c) 2013-2022, iLert GmbH. <support@ilert.com>
# All rights reserved.


import os
import sys
import syslog
import datetime
import fcntl
import urllib.request
from urllib.request import HTTPError
from urllib.error import URLError
import uuid
from xml.sax.saxutils import escape
from xml.sax.saxutils import quoteattr
import argparse
import io

PLUGIN_VERSION = "1.5"


def log(level, message):
    docker_mode = os.getenv("DOCKER_MODE", "false")
    if docker_mode == "yes" or docker_mode == "true" or docker_mode == "y":
        if level == "ERROR":
            sys.stderr.write("%s %s %s\n" % (datetime.datetime.now().isoformat(), level, message))
        else:
            sys.stdout.write("%s %s %s\n" % (datetime.datetime.now().isoformat(), level, message))
    else:
        if level == "ERROR":
            syslog.syslog(syslog.LOG_ERR, "%s %s %s" % (datetime.datetime.now().isoformat(), level, message))
        elif level == "WARN":
            syslog.syslog(syslog.LOG_WARNING, "%s %s %s" % (datetime.datetime.now().isoformat(), level, message))
        else:
            syslog.syslog(syslog.LOG_INFO, "%s %s %s" % (datetime.datetime.now().isoformat(), level, message))


def persist_event(api_key, directory, payload):
    """Persists event to disk"""
    log("INFO", "writing event to disk...")
    log("INFO", payload)

    xml_doc = create_xml(api_key, payload)

    uid = uuid.uuid4()

    filename = "%s.ilert" % uid
    filename_tmp = "%s.tmp" % uid
    file_path = "%s/%s" % (directory, filename)
    file_path_tmp = "%s/%s" % (directory, filename_tmp)

    try:
        # atomic write using tmp file, see http://stackoverflow.com/questions/2333872
        with io.open(file_path_tmp, mode="w", encoding="utf-8") as f:
            f.write(xml_doc)
            # make sure all data is on disk
            f.flush()
            # skip os.sync in favor of performance/responsiveness
            # os.fsync(f.fileno())
            f.close()
            os.rename(file_path_tmp, file_path)
            log("INFO", "created event file in %s" % file_path)
    except Exception as e:
        log("ERROR", "could not write event to %s. Cause: %s %s" % (file_path, type(e), e.args))
        exit(1)


def lock_and_flush(endpoint, directory, port):
    """Lock event directory and call flush"""
    lock_filename = "%s/lockfile" % directory

    lockfile = open(lock_filename, "w")

    try:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        flush(endpoint, directory, port)
    finally:
        lockfile.close()


def flush(endpoint, directory, port):
    """Send all events in event directory to iLert"""
    headers = {"Content-type": "application/xml", "Accept": "application/xml"}
    url = "%s:%s/api/v1/events/icinga" % (endpoint, port)

    # populate list of event files sorted by creation date
    events = [os.path.join(directory, f) for f in os.listdir(directory)]
    events = list(filter(lambda x: x.endswith(".ilert"), events))
    events = sorted(events, key=lambda x: os.path.getmtime(x))

    for event in events:
        try:
            with open(event, 'r', encoding='utf-8') as f:
                xml_doc = f.read()
        except IOError:
            continue

        log("INFO", "sending event %s to iLert..." % event)

        try:
            data = xml_doc.encode('utf-8')
            req = urllib.request.Request(url, data, headers)
            urllib.request.urlopen(req, timeout=60)
        except HTTPError as e:
            if e.code == 429:
                log("WARN", "too many requests, will try later. Server response: %s" % e.read())
            elif 400 <= e.code <= 499:
                log("WARN", "event not accepted by iLert. Reason: %s\n" % e.read())
                os.remove(event)
            else:
                sys.stderr.write("could not send event to iLert. HTTP error code %s, reason: %s, %s" % (
                    e.code, e.reason, e.read()))
        except URLError as e:
            log("ERROR", "could not send event to iLert. Reason: %s\n" % e.reason)
        except Exception as e:
            log("ERROR", "an unexpected error occurred. Please report a bug. Cause: %s %s" % (type(e), e.args))
        else:
            os.remove(event)
            log("INFO", "event %s has been sent to iLert and removed from event directory" % event)


def create_xml(apikey, payload):
    """Create event xml using the provided api key and event payload"""
    xml_doc = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    xml_doc += "<event><apiKey>%s</apiKey><payload>" % apikey

    for entry in payload:
        xml_doc += "<entry key=%s>%s</entry>" % (quoteattr(entry), escape(payload[entry]))

    # XML document end tag
    xml_doc += "</payload></event>"

    return xml_doc


def main():
    parser = argparse.ArgumentParser(description='send events from Icinga to iLert')
    parser.add_argument('-m', '--mode', choices=['icinga', 'save', 'cron', 'send'], required=True,
                        help='Execution mode: "save" persists an event to disk and "send" submits all saved events '
                             'to iLert. Note that after every "save" "send" will also be called.')
    parser.add_argument('-a', '--apikey', help='API key for the alert source in iLert')
    parser.add_argument('-e', '--endpoint', default='https://api.ilert.com',
                        help='iLert API endpoint (default: %(default)s)')
    parser.add_argument('-p', '--port', type=int, default=443, help='endpoint port (default: %(default)s)')
    parser.add_argument('-d', '--dir', default='/tmp/ilert-icinga',
                        help='event directory where events are stored (default: %(default)s)')
    parser.add_argument('--version', action='version', version=PLUGIN_VERSION)
    parser.add_argument('payload', nargs=argparse.REMAINDER,
                        help='event payload as key value pairs in the format key1=value1 key2=value2 ...')
    args = parser.parse_args()

    # populate payload data from environment variables
    payload = dict(PLUGIN_VERSION=PLUGIN_VERSION)
    for env in os.environ:
        if "ICINGA_" in env or "NOTIFY_" in env:
            payload[env] = os.environ[env]

    # ... and payload specified via command line
    payload.update([arg.split('=', 1) for arg in args.payload])

    if args.apikey is not None:
        apikey = args.apikey
    elif 'ICINGA_CONTACTPAGER' in payload:
        apikey = payload['ICINGA_CONTACTPAGER']
    elif 'CONTACTPAGER' in payload:
        apikey = payload['CONTACTPAGER']
    else:
        apikey = None

    if not os.path.exists(args.dir):
        os.makedirs(args.dir)

    if args.mode == "icinga" or args.mode == "save":
        if apikey is None:
            error_msg = "parameter apikey is required in save mode and must be provided either via command line or in " \
                        "the pager field of the contact definition in Icinga"
            log("ERROR", error_msg)
            parser.error(error_msg)
        persist_event(apikey, args.dir, payload)
        lock_and_flush(args.endpoint, args.dir, args.port)
    elif args.mode == "cron" or args.mode == "send":
        lock_and_flush(args.endpoint, args.dir, args.port)

    exit(0)


if __name__ == '__main__':
    main()