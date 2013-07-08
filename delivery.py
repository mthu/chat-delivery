#!/usr/bin/env python
#coding: utf-8

'''
Chat delivery. Delivers archived messages from Prosody Archive (mod_mam_sql) to users' mailboxes.

Copyright (c) 2013, Martin Plicka
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

 * Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
 * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''

import cgi
from smtplib import LMTP

import time
import pytz
from datetime import datetime, timedelta
from dateutil.tz import tzlocal

from email import charset
from email.encoders import encode_quopri
from email.header import Header
from email.utils import formatdate
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import sys
import re
import simplejson
import MySQLdb as mysql

from getopt import getopt
from ConfigParser import RawConfigParser, _default_dict

DEFAULT_CONFIG_FILE = '/etc/prosody/chat-delivery.conf'
DEFAULT_CONFIG = {
    'general': {
        'message_delay': 60,
        'your_username': 'You',
    },
    'delivery': {
        'prefer_qp': True,
        'method': 'lmtp',
    },
    'sql': {
        'prosody_config': '/etc/prosody/prosody.cfg.lua',
        'host': 'localhost',
        'user': 'prosody',
        'db': 'prosody',
    }
}

config = None # to be loaded in loadConfig()
dbCon = None # to be connected in setUp()

DRY_RUN = False #change to True or use -n command-line option to not to deliver emails and save checkpoint


def saveCheckPoint(user, buddy, lastMessageId):
    '''
        Save last processed message ID for each buddy to database.
    '''
    if DRY_RUN:
        return
    username, host = user.split('@')
    cur = dbCon.cursor()
    cur.execute("""
        UPDATE prosodyarchive_checkpoint
        SET `last_id` = %s
        WHERE `host` = %s and `user` = %s and `with` = %s
    """, (lastMessageId, host, username, buddy))
    if cur.rowcount < 1:
        cur.execute("""
            INSERT INTO prosodyarchive_checkpoint (`host`, `user`, `with`, `last_id`)
            VALUES (%s, %s, %s, %s)
        """, (host, username, buddy, lastMessageId))
    dbCon.commit()


def getBuddiesForUser(user):
    '''
        Load buddies for given user from roster.
    '''
    username, host = user.split('@')
    cur = dbCon.cursor()
    cur.execute("""
        select p.`key`, p.`value`, c.`last_id`
        from prosody p
        left join prosodyarchive_checkpoint c on
            (p.`host` = c.`host` and p.`user` = c.`user` and p.`key` = c.`with`)
        where
            p.`user` = %s
            and p.`host` = %s
            and p.`store` = 'roster'
            and p.`key` like '%%@%%.%%'
    """, (username, host))
    def extractName(value, default):
        return simplejson.loads(value).get('name', default)
    return [{'user': x[0], 'name': extractName(x[1],x[0]), 'last_message_id': int(x[2] or 0)} for x in cur.fetchall()]


def extractMessageBody(parsed):
    return "\n".join((x.get('__array') for x in parsed['__array'] if isinstance(x, dict) and x.get('name') == 'body').next()).strip()


def getBuddyMessages(user, buddy):
    username, host = user.split('@')
    cur = dbCon.cursor()
    cur.execute("""
         select
             `id`,
             `when`,
             `resource`,
             `stanza`
         from prosodyarchive
         where
             `user` = %s
             and `host` = %s
             and `store` = 'archive2'
             and `with` = %s
             and `id` > %s
        order by `id`
     """, (
        username,
        host,
        buddy['user'],
        buddy['last_message_id']
    ))
    messages = []
    for row in cur.fetchall():
        parsed = simplejson.loads(row[3])
        messages.append({
            'id': row[0],
            'from': parsed['attr']['from'].split("/")[0],
            'to': parsed['attr']['to'].split("/")[0],
            'time': datetime.fromtimestamp(row[1], tzlocal()),
            'body': extractMessageBody(parsed),
        })
    return messages


def getUserName(user):
    '''
        TODO: load from user's vcard
    '''
    return config.get('general', 'your_username')


def groupMessages(msgList):
    if not msgList:
        return []
    messageDelay = timedelta(minutes=config.getint('general', 'message_delay'))
    now = datetime.now(tzlocal())

    output = []
    current = msgList[:1]
    last = msgList[0]['time']
    for msg in msgList[1:]:
        if msg['time'] > last + messageDelay:
            output.append(current)
            current = [msg]
        else:
            current.append(msg)
        last = msg['time']
    if current and last + messageDelay < now:
        output.append(current)
    return output


def formatLinks(message):
    message = re.sub(r'(?P<begin>(^|[ ]))(?P<link>(http|https|ftp)://[^/ \n]*(/[^/ \n]*)*[^., \n])(?P<end>[,.]*($|[ ]))', r'\g<begin><a href="\g<link>">\g<link></a>\g<end>', message, flags=re.M | re.I)
    message = re.sub(r'\n', r'<br>', message, flags=re.M)
    return message


def formatTime(dt):
    return dt.astimezone(pytz.timezone(config.get('general', 'users_timezone'))).strftime('%H:%M')


def deliverMessages(user, userNick, buddy, buddyNick, messages, logSource='Prosody import'):
    msg = MIMEMultipart('alternative')

    msg['From'] = "%s <%s>" % (Header(buddyNick, 'utf-8'), buddy)
    msg['To'] = "%s <%s>" % (Header(userNick, 'utf-8'), user)
    msg['X-Xmpp-History'] = logSource
    msg['Date'] = formatdate(time.mktime(messages[-1]['time'].timetuple()), True)
    msg['Subject'] = Header(u"Chat with %s" % buddyNick, 'utf-8')

    text = "\n".join("<%(time)s> *%(nick)s*: %(body)s" % {
                            'time': formatTime(msg['time']),
                            'nick': userNick if msg['from'] == user else buddyNick,
                            'body': msg['body']
                        } for msg in messages)

    html = """\
<html>
    <head></head>
    <body>
        %s
    </body>
</html>""" % "\n".join("<div>&lt;%(time)s&gt; <b>%(nick)s</b>: %(body)s</div>" % {
                            'time': formatTime(msg['time']),
                            'nick': cgi.escape(userNick if msg['from'] == user else buddyNick),
                            'body': formatLinks(cgi.escape(msg['body']))
                        } for msg in messages)
    jsonInput = [
        {
            'from': message['from'],
            'to': message['to'],
            'time': message['time'].isoformat(),
            'body': message['body']
        } for message in messages]
    json = simplejson.dumps(jsonInput, ensure_ascii=False, separators=(',',':'))

    msg.attach(MIMEText(text, 'plain', 'utf-8'))
    msg.attach(MIMEText(json, 'x-json', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8')) #the default one should be the last

    if DRY_RUN:
        print msg.as_string()
        return
    assert config.get('delivery', 'method').lower() == 'lmtp', "Only LMTP delivery is yet supported"
    target = config.get('users', user) or user
    client = LMTP(config.get('delivery_lmtp', 'address'))
    client.sendmail(target, target, msg.as_string())


def main(args):
    for user, _target in config.items('users'):
        for buddy in getBuddiesForUser(user):
            messages = getBuddyMessages(user, buddy)
            grouped = groupMessages(messages)
            for group in grouped:
                deliverMessages(user, getUserName(user), buddy['user'], buddy['name'], group)
            if grouped:
                saveCheckPoint(user, buddy['user'], grouped[-1][-1]['id'])


def setUp(opts):
    '''
        Set up running environment.
            1. Email encoder
            2. SQL connection
            3. DRY RUN global variable
            ...
    '''
    if config.getboolean('delivery', 'prefer_qp'):
        #enforce quoted printable encoding for utf-8 emails
        charset.add_charset('utf-8', charset.QP, charset.QP)
    try:
        sql = readProsodySqlConfig(config.get('sql', 'prosody_config'))
        assert sql['driver'].lower() == 'mysql', "Only MySQL mod_mam_sql backend yet supported"
        config.read_dict({
            'sql': {
                'db': sql['database'],
                'user': sql['username'],
                'passwd': sql['password'],
                'host': sql['host'],
            }
        })
    except: #TODO: better exception handling
        pass

    global dbCon
    dbCon = mysql.connect(
        host=config.get('sql', 'host'),
        db=config.get('sql', 'db'),
        user=config.get('sql', 'user'),
        passwd=config.get('sql', 'passwd')
    )

    if '-n' in opts or '--dry-run' in opts:
        global DRY_RUN
        DRY_RUN = True


def readProsodySqlConfig(filename):
    '''
        Try to read database credentials from Prosody config.
        Not so clever but it should work in most cases.
        Example config file contents:
        sql = { driver = "MySQL", database = "prosody", username = "prosody", password = "secret_password", host = "localhost" }
    '''
    with open(filename, 'r') as configFile:
        conf = configFile.read()
    #try to find "sql = {....}" section (uncommented)
    result = re.findall(r'\n\s*sql\s*=\s({[^}]*})', conf)
    assert len(result) == 1, "Number of found sql config sections should be 1"

    #patch it to be compatible with json parser
    patched = re.sub(r'\s([\w]{1,})\s=\s"', r' "\1" : "', result[0])
    #parse as json
    return simplejson.loads(patched)


class Config(RawConfigParser):
    '''
        'Improved' ConfigParser.
        It allows preload config from dictionary.
    '''

    def read_dict(self, structured_config):
        if structured_config:
            for section, items in structured_config.iteritems():
                if not self.has_section(section):
                    self.add_section(section)
                for item, value in items.iteritems():
                    self.set(section, item, value)


def loadConfig(configFile):
    '''
        Initialize config object.
        Load defaults from DEFAULT_CONFIG dictionary + config from file
    '''
    global config
    config = Config(allow_no_value=True)
    config.read_dict(DEFAULT_CONFIG) #load defaults
    if not config.read(configFile):
        raise Exception("Config file not found: %s, check -c option" % configFile)
    return config


def getConfigFile(opts):
    '''
        Determine config filename from command-line arguments or use default path
    '''
    configFile = DEFAULT_CONFIG_FILE
    if '-c' in opts:
        if '--config' in opts:
            raise Exception("Conflict between -c and --config directive")
        configFile = opts['-c']
    if '--config' in opts:
        configFile = opts['--config']
    return configFile


if __name__ == "__main__":
    opts, args = getopt(sys.argv[1:], "c:n", ["config=", "dry-run"])
    opts = dict(opts)
    loadConfig(getConfigFile(opts))
    setUp(opts)
    retValue = main(args)
    sys.exit(retValue)

