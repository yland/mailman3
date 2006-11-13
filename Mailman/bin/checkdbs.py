# Copyright (C) 1998-2006 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

import sys
import time
import optparse

from email.Charset import Charset

from Mailman import MailList
from Mailman import Message
from Mailman import Utils
from Mailman import Version
from Mailman import i18n
from Mailman.configuration import config

_ = i18n._
__i18n_templates__ = True

# Work around known problems with some RedHat cron daemons
import signal
signal.signal(signal.SIGCHLD, signal.SIG_DFL)

NL = u'\n'
now = time.time()



def parseargs():
    parser = optparse.OptionParser(version=Version.MAILMAN_VERSION,
                                   usage=_("""\
%prog [options]

Check for pending admin requests and mail the list owners if necessary."""))
    parser.add_option('-C', '--config',
                      help=_('Alternative configuration file to use'))
    opts, args = parser.parse_args()
    if args:
        parser.print_help()
        print >> sys.stderr, _('Unexpected arguments')
        sys.exit(1)
    return opts, args, parser



def pending_requests(mlist):
    # Must return a byte string
    lcset = Utils.GetCharSet(mlist.preferred_language)
    pending = []
    first = True
    for id in mlist.GetSubscriptionIds():
        if first:
            pending.append(_('Pending subscriptions:'))
            first = False
        when, addr, fullname, passwd, digest, lang = mlist.GetRecord(id)
        if fullname:
            if isinstance(fullname, unicode):
                fullname = fullname.encode(lcset, 'replace')
            fullname = ' (%s)' % fullname
        pending.append('    %s%s %s' % (addr, fullname, time.ctime(when)))
    first = True
    for id in mlist.GetHeldMessageIds():
        if first:
            pending.append(_('\nPending posts:'))
            first = False
        info = mlist.GetRecord(id)
        when, sender, subject, reason, text, msgdata = mlist.GetRecord(id)
        subject = Utils.oneline(subject, lcset)
        date = time.ctime(when)
        reason = _(reason)
        pending.append(_("""\
From: $sender on $date
Subject: $subject
Cause: $reason"""))
        pending.append('')
    # Coerce all items in pending to a Unicode so we can join them
    upending = []
    charset = Utils.GetCharSet(mlist.preferred_language)
    for s in pending:
        if isinstance(s, unicode):
            upending.append(s)
        else:
            upending.append(unicode(s, charset, 'replace'))
    # Make sure that the text we return from here can be encoded to a byte
    # string in the charset of the list's language.  This could fail if for
    # example, the request was pended while the list's language was French,
    # but then it was changed to English before checkdbs ran.
    text = NL.join(upending)
    charset = Charset(Utils.GetCharSet(mlist.preferred_language))
    incodec = charset.input_codec or 'ascii'
    outcodec = charset.output_codec or 'ascii'
    if isinstance(text, unicode):
        return text.encode(outcodec, 'replace')
    # Be sure this is a byte string encodeable in the list's charset
    utext = unicode(text, incodec, 'replace')
    return utext.encode(outcodec, 'replace')



def auto_discard(mlist):
    # Discard old held messages
    discard_count = 0
    expire = config.days(mlist.max_days_to_hold)
    heldmsgs = mlist.GetHeldMessageIds()
    if expire and heldmsgs:
        for id in heldmsgs:
            if now - mlist.GetRecord(id)[0] > expire:
                mlist.HandleRequest(id, config.DISCARD)
                discard_count += 1
        mlist.Save()
    return discard_count



def main():
    opts, args, parser = parseargs()
    config.load(opts.config)

    i18n.set_language(config.DEFAULT_SERVER_LANGUAGE)

    for name in Utils.list_names():
        # The list must be locked in order to open the requests database
        mlist = MailList.MailList(name)
        try:
            count = mlist.NumRequestsPending()
            # While we're at it, let's evict yesterday's autoresponse data
            midnight_today = Utils.midnight()
            evictions = []
            for sender in mlist.hold_and_cmd_autoresponses.keys():
                date, respcount = mlist.hold_and_cmd_autoresponses[sender]
                if Utils.midnight(date) < midnight_today:
                    evictions.append(sender)
            if evictions:
                for sender in evictions:
                    del mlist.hold_and_cmd_autoresponses[sender]
                # This is the only place we've changed the list's database
                mlist.Save()
            if count:
                i18n.set_language(mlist.preferred_language)
                realname = mlist.real_name
                discarded = auto_discard(mlist)
                if discarded:
                    count = count - discarded
                    text = _(
                 'Notice: $discarded old request(s) automatically expired.\n\n')
                else:
                    text = ''
                if count:
                    text += Utils.maketext(
                        'checkdbs.txt',
                        {'count'    : count,
                         'host_name': mlist.host_name,
                         'adminDB'  : mlist.GetScriptURL('admindb', absolute=1),
                         'real_name': realname,
                         }, mlist=mlist)
                    text += '\n' + pending_requests(mlist)
                    subject = _('$count $realname moderator request(s) waiting')
                else:
                    subject = _('$realname moderator request check result')
                msg = Message.UserNotification(mlist.GetOwnerEmail(),
                                               mlist.GetBouncesEmail(),
                                               subject, text,
                                               mlist.preferred_language)
                msg.send(mlist, **{'tomoderators': True})
        finally:
            mlist.Unlock()



if __name__ == '__main__':
    main()