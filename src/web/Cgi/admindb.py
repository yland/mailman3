# Copyright (C) 1998-2009 by the Free Software Foundation, Inc.
#
# This file is part of GNU Mailman.
#
# GNU Mailman is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# GNU Mailman is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# GNU Mailman.  If not, see <http://www.gnu.org/licenses/>.

"""Produce and process the pending-approval items for a list."""

import os
import cgi
import sys
import time
import email
import errno
import logging

from urllib import quote_plus, unquote_plus

from Mailman import Errors
from Mailman import MailList
from Mailman import Message
from Mailman import Utils
from Mailman import i18n
from Mailman.Cgi import Auth
from Mailman.Handlers.Moderate import ModeratedMemberPost
from Mailman.ListAdmin import readMessage
from Mailman.configuration import config
from Mailman.htmlformat import *
from Mailman.interfaces import RequestType

EMPTYSTRING = ''
NL = '\n'

# Set up i18n.  Until we know which list is being requested, we use the
# server's default.
_ = i18n._
i18n.set_language(config.DEFAULT_SERVER_LANGUAGE)

EXCERPT_HEIGHT = 10
EXCERPT_WIDTH = 76

log = logging.getLogger('mailman.error')



def helds_by_sender(mlist):
    bysender = {}
    requests = config.db.get_list_requests(mlist)
    for request in requests.of_type(RequestType.held_message):
        key, data = requests.get_request(request.id)
        sender = data.get('sender')
            assert sender is not None, (
                'No sender for held message: %s' % request.id)
            bysender.setdefault(sender, []).append(request.id)
    return bysender


def hacky_radio_buttons(btnname, labels, values, defaults, spacing=3):
    # We can't use a RadioButtonArray here because horizontal placement can be
    # confusing to the user and vertical placement takes up too much
    # real-estate.  This is a hack!
    space = '&nbsp;' * spacing
    btns = Table(cellspacing='5', cellpadding='0')
    btns.AddRow([space + text + space for text in labels])
    btns.AddRow([Center(RadioButton(btnname, value, default))
                 for value, default in zip(values, defaults)])
    return btns



def main():
    # Figure out which list is being requested
    parts = Utils.GetPathPieces()
    if not parts:
        handle_no_list()
        return

    listname = parts[0].lower()
    try:
        mlist = MailList.MailList(listname, lock=0)
    except Errors.MMListError, e:
        # Avoid cross-site scripting attacks
        safelistname = Utils.websafe(listname)
        handle_no_list(_('No such list <em>%(safelistname)s</em>'))
        log.error('No such list "%s": %s\n', listname, e)
        return

    # Now that we know which list to use, set the system's language to it.
    i18n.set_language(mlist.preferred_language)

    # Make sure the user is authorized to see this page.
    cgidata = cgi.FieldStorage(keep_blank_values=1)

    if not mlist.WebAuthenticate((config.AuthListAdmin,
                                  config.AuthListModerator,
                                  config.AuthSiteAdmin),
                                 cgidata.getvalue('adminpw', '')):
        if cgidata.has_key('adminpw'):
            # This is a re-authorization attempt
            msg = Bold(FontSize('+1', _('Authorization failed.'))).Format()
        else:
            msg = ''
        Auth.loginpage(mlist, 'admindb', msg=msg)
        return

    # Set up the results document
    doc = Document()
    doc.set_language(mlist.preferred_language)

    # See if we're requesting all the messages for a particular sender, or if
    # we want a specific held message.
    sender = None
    msgid = None
    details = None
    envar = os.environ.get('QUERY_STRING')
    if envar:
        # POST methods, even if their actions have a query string, don't get
        # put into FieldStorage's keys :-(
        qs = cgi.parse_qs(envar).get('sender')
        if qs and isinstance(qs, list):
            sender = qs[0]
        qs = cgi.parse_qs(envar).get('msgid')
        if qs and isinstance(qs, list):
            msgid = qs[0]
        qs = cgi.parse_qs(envar).get('details')
        if qs and isinstance(qs, list):
            details = qs[0]

    mlist.Lock()
    try:
        realname = mlist.real_name
        if not cgidata.keys() or cgidata.has_key('admlogin'):
            # If this is not a form submission (i.e. there are no keys in the
            # form) or it's a login, then we don't need to do much special.
            doc.SetTitle(_('%(realname)s Administrative Database'))
        elif not details:
            # This is a form submission
            doc.SetTitle(_('%(realname)s Administrative Database Results'))
            process_form(mlist, doc, cgidata)
        # Now print the results and we're done.  Short circuit for when there
        # are no pending requests, but be sure to save the results!
        if config.db.requests.get_list_requests(mlist).count == 0:
            title = _('%(realname)s Administrative Database')
            doc.SetTitle(title)
            doc.AddItem(Header(2, title))
            doc.AddItem(_('There are no pending requests.'))
            doc.AddItem(' ')
            doc.AddItem(Link(mlist.GetScriptURL('admindb'),
                             _('Click here to reload this page.')))
            doc.AddItem(mlist.GetMailmanFooter())
            print doc.Format()
            mlist.Save()
            return

        admindburl = mlist.GetScriptURL('admindb')
        form = Form(admindburl)
        # Add the instructions template
        if details == 'instructions':
            doc.AddItem(Header(
                2, _('Detailed instructions for the administrative database')))
        else:
            doc.AddItem(Header(
                2,
                _('Administrative requests for mailing list:')
                + ' <em>%s</em>' % mlist.real_name))
        if details <> 'instructions':
            form.AddItem(Center(SubmitButton('submit', _('Submit All Data'))))
        requestsdb = config.db.get_list_requests(mlist)
        message_count = requestsdb.count_of(RequestType.held_message)
        if not (details or sender or msgid or message_count == 0):
            form.AddItem(Center(
                CheckBox('discardalldefersp', 0).Format() +
                '&nbsp;' +
                _('Discard all messages marked <em>Defer</em>')
                ))
        # Add a link back to the overview, if we're not viewing the overview!
        adminurl = mlist.GetScriptURL('admin')
        d = {'listname'  : mlist.real_name,
             'detailsurl': admindburl + '?details=instructions',
             'summaryurl': admindburl,
             'viewallurl': admindburl + '?details=all',
             'adminurl'  : adminurl,
             'filterurl' : adminurl + '/privacy/sender',
             }
        addform = 1
        if sender:
            esender = Utils.websafe(sender)
            d['description'] = _("all of %(esender)s's held messages.")
            doc.AddItem(Utils.maketext('admindbpreamble.html', d,
                                       raw=1, mlist=mlist))
            show_sender_requests(mlist, form, sender)
        elif msgid:
            d['description'] = _('a single held message.')
            doc.AddItem(Utils.maketext('admindbpreamble.html', d,
                                       raw=1, mlist=mlist))
            show_message_requests(mlist, form, msgid)
        elif details == 'all':
            d['description'] = _('all held messages.')
            doc.AddItem(Utils.maketext('admindbpreamble.html', d,
                                       raw=1, mlist=mlist))
            show_detailed_requests(mlist, form)
        elif details == 'instructions':
            doc.AddItem(Utils.maketext('admindbdetails.html', d,
                                       raw=1, mlist=mlist))
            addform = 0
        else:
            # Show a summary of all requests
            doc.AddItem(Utils.maketext('admindbsummary.html', d,
                                       raw=1, mlist=mlist))
            num = show_pending_subs(mlist, form)
            num += show_pending_unsubs(mlist, form)
            num += show_helds_overview(mlist, form)
            addform = num > 0
        # Finish up the document, adding buttons to the form
        if addform:
            doc.AddItem(form)
            form.AddItem('<hr>')
            if not (details or sender or msgid or nomessages):
                form.AddItem(Center(
                    CheckBox('discardalldefersp', 0).Format() +
                    '&nbsp;' +
                    _('Discard all messages marked <em>Defer</em>')
                    ))
            form.AddItem(Center(SubmitButton('submit', _('Submit All Data'))))
        doc.AddItem(mlist.GetMailmanFooter())
        print doc.Format()
        # Commit all changes
        mlist.Save()
    finally:
        mlist.Unlock()



def handle_no_list(msg=''):
    # Print something useful if no list was given.
    doc = Document()
    doc.set_language(config.DEFAULT_SERVER_LANGUAGE)

    header = _('Mailman Administrative Database Error')
    doc.SetTitle(header)
    doc.AddItem(Header(2, header))
    doc.AddItem(msg)
    url = Utils.ScriptURL('admin')
    link = Link(url, _('list of available mailing lists.')).Format()
    doc.AddItem(_('You must specify a list name.  Here is the %(link)s'))
    doc.AddItem('<hr>')
    doc.AddItem(MailmanLogo())
    print doc.Format()



def show_pending_subs(mlist, form):
    # Add the subscription request section
    requestsdb = config.db.get_list_requests(mlist)
    if requestsdb.count_of(RequestType.subscription) == 0:
        return 0
    form.AddItem('<hr>')
    form.AddItem(Center(Header(2, _('Subscription Requests'))))
    table = Table(border=2)
    table.AddRow([Center(Bold(_('Address/name'))),
                  Center(Bold(_('Your decision'))),
                  Center(Bold(_('Reason for refusal')))
                  ])
    # Alphabetical order by email address
    byaddrs = {}
    for request in requestsdb.of_type(RequestType.subscription):
        key, data = requestsdb.get_request(requst.id)
        addr = data['addr']
        byaddrs.setdefault(addr, []).append(request.id)
    addrs = sorted(byaddrs)
    num = 0
    for addr, ids in byaddrs.items():
        # Eliminate duplicates
        for id in ids[1:]:
            mlist.HandleRequest(id, config.DISCARD)
        id = ids[0]
        key, data = requestsdb.get_request(id)
        time = data['time']
        addr = data['addr']
        fullname = data['fullname']
        passwd = data['passwd']
        digest = data['digest']
        lang = data['lang']
        fullname = Utils.uncanonstr(fullname, mlist.preferred_language)
        radio = RadioButtonArray(id, (_('Defer'),
                                      _('Approve'),
                                      _('Reject'),
                                      _('Discard')),
                                 values=(config.DEFER,
                                         config.SUBSCRIBE,
                                         config.REJECT,
                                         config.DISCARD),
                                 checked=0).Format()
        if addr not in mlist.ban_list:
            radio += '<br>' + CheckBox('ban-%d' % id, 1).Format() + \
                     '&nbsp;' + _('Permanently ban from this list')
        # While the address may be a unicode, it must be ascii
        paddr = addr.encode('us-ascii', 'replace')
        table.AddRow(['%s<br><em>%s</em>' % (paddr, fullname),
                      radio,
                      TextBox('comment-%d' % id, size=40)
                      ])
        num += 1
    if num > 0:
        form.AddItem(table)
    return num



def show_pending_unsubs(mlist, form):
    # Add the pending unsubscription request section
    lang = mlist.preferred_language
    requestsdb = config.db.get_list_requests(mlist)
    if requestsdb.count_of(RequestType.unsubscription) == 0:
        return 0
    table = Table(border=2)
    table.AddRow([Center(Bold(_('User address/name'))),
                  Center(Bold(_('Your decision'))),
                  Center(Bold(_('Reason for refusal')))
                  ])
    # Alphabetical order by email address
    byaddrs = {}
    for request in requestsdb.of_type(RequestType.unsubscription):
        key, data = requestsdb.get_request(request.id)
        addr = data['addr']
        byaddrs.setdefault(addr, []).append(request.id)
    addrs = sorted(byaddrs)
    num = 0
    for addr, ids in byaddrs.items():
        # Eliminate duplicates
        for id in ids[1:]:
            mlist.HandleRequest(id, config.DISCARD)
        id = ids[0]
        key, data = requestsdb.get_record(id)
        addr = data['addr']
        try:
            fullname = Utils.uncanonstr(mlist.getMemberName(addr), lang)
        except Errors.NotAMemberError:
            # They must have been unsubscribed elsewhere, so we can just
            # discard this record.
            mlist.HandleRequest(id, config.DISCARD)
            continue
        num += 1
        table.AddRow(['%s<br><em>%s</em>' % (addr, fullname),
                      RadioButtonArray(id, (_('Defer'),
                                            _('Approve'),
                                            _('Reject'),
                                            _('Discard')),
                                       values=(config.DEFER,
                                               config.UNSUBSCRIBE,
                                               config.REJECT,
                                               config.DISCARD),
                                       checked=0),
                      TextBox('comment-%d' % id, size=45)
                      ])
    if num > 0:
        form.AddItem('<hr>')
        form.AddItem(Center(Header(2, _('Unsubscription Requests'))))
        form.AddItem(table)
    return num



def show_helds_overview(mlist, form):
    # Sort the held messages by sender
    bysender = helds_by_sender(mlist)
    if not bysender:
        return 0
    form.AddItem('<hr>')
    form.AddItem(Center(Header(2, _('Held Messages'))))
    # Add the by-sender overview tables
    admindburl = mlist.GetScriptURL('admindb')
    table = Table(border=0)
    form.AddItem(table)
    senders = bysender.keys()
    senders.sort()
    for sender in senders:
        qsender = quote_plus(sender)
        esender = Utils.websafe(sender)
        senderurl = admindburl + '?sender=' + qsender
        # The encompassing sender table
        stable = Table(border=1)
        stable.AddRow([Center(Bold(_('From:')).Format() + esender)])
        stable.AddCellInfo(stable.GetCurrentRowIndex(), 0, colspan=2)
        left = Table(border=0)
        left.AddRow([_('Action to take on all these held messages:')])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        btns = hacky_radio_buttons(
            'senderaction-' + qsender,
            (_('Defer'), _('Accept'), _('Reject'), _('Discard')),
            (config.DEFER, config.APPROVE, config.REJECT, config.DISCARD),
            (1, 0, 0, 0))
        left.AddRow([btns])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        left.AddRow([
            CheckBox('senderpreserve-' + qsender, 1).Format() +
            '&nbsp;' +
            _('Preserve messages for the site administrator')
            ])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        left.AddRow([
            CheckBox('senderforward-' + qsender, 1).Format() +
            '&nbsp;' +
            _('Forward messages (individually) to:')
            ])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        left.AddRow([
            TextBox('senderforwardto-' + qsender,
                    value=mlist.GetOwnerEmail())
            ])
        left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        # If the sender is a member and the message is being held due to a
        # moderation bit, give the admin a chance to clear the member's mod
        # bit.  If this sender is not a member and is not already on one of
        # the sender filters, then give the admin a chance to add this sender
        # to one of the filters.
        if mlist.isMember(sender):
            if mlist.getMemberOption(sender, config.Moderate):
                left.AddRow([
                    CheckBox('senderclearmodp-' + qsender, 1).Format() +
                    '&nbsp;' +
                    _("Clear this member's <em>moderate</em> flag")
                    ])
            else:
                left.AddRow(
                    [_('<em>The sender is now a member of this list</em>')])
            left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        elif sender not in (mlist.accept_these_nonmembers +
                            mlist.hold_these_nonmembers +
                            mlist.reject_these_nonmembers +
                            mlist.discard_these_nonmembers):
            left.AddRow([
                CheckBox('senderfilterp-' + qsender, 1).Format() +
                '&nbsp;' +
                _('Add <b>%(esender)s</b> to one of these sender filters:')
                ])
            left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
            btns = hacky_radio_buttons(
                'senderfilter-' + qsender,
                (_('Accepts'), _('Holds'), _('Rejects'), _('Discards')),
                (config.ACCEPT, config.HOLD, config.REJECT, config.DISCARD),
                (0, 0, 0, 1))
            left.AddRow([btns])
            left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
            if sender not in mlist.ban_list:
                left.AddRow([
                    CheckBox('senderbanp-' + qsender, 1).Format() +
                    '&nbsp;' +
                    _("""Ban <b>%(esender)s</b> from ever subscribing to this
                    mailing list""")])
                left.AddCellInfo(left.GetCurrentRowIndex(), 0, colspan=2)
        right = Table(border=0)
        right.AddRow([
            _("""Click on the message number to view the individual
            message, or you can """) +
            Link(senderurl, _('view all messages from %(esender)s')).Format()
            ])
        right.AddCellInfo(right.GetCurrentRowIndex(), 0, colspan=2)
        right.AddRow(['&nbsp;', '&nbsp;'])
        counter = 1
        for id in bysender[sender]:
            key, data = requestsdb.get_record(id)
            ptime = data['ptime']
            sender = data['sender']
            subject = data['subject']
            reason = data['reason']
            filename = data['filename']
            msgdata = data['msgdata']
            # BAW: This is really the size of the message pickle, which should
            # be close, but won't be exact.  Sigh, good enough.
            try:
                size = os.path.getsize(os.path.join(config.DATA_DIR, filename))
            except OSError, e:
                if e.errno <> errno.ENOENT: raise
                # This message must have gotten lost, i.e. it's already been
                # handled by the time we got here.
                mlist.HandleRequest(id, config.DISCARD)
                continue
            dispsubj = Utils.oneline(
                subject, Utils.GetCharSet(mlist.preferred_language))
            t = Table(border=0)
            t.AddRow([Link(admindburl + '?msgid=%d' % id, '[%d]' % counter),
                      Bold(_('Subject:')),
                      Utils.websafe(dispsubj)
                      ])
            t.AddRow(['&nbsp;', Bold(_('Size:')), str(size) + _(' bytes')])
            if reason:
                reason = _(reason)
            else:
                reason = _('not available')
            t.AddRow(['&nbsp;', Bold(_('Reason:')), reason])
            # Include the date we received the message, if available
            when = msgdata.get('received_time')
            if when:
                t.AddRow(['&nbsp;', Bold(_('Received:')),
                          time.ctime(when)])
            counter += 1
            right.AddRow([t])
        stable.AddRow([left, right])
        table.AddRow([stable])
    return 1



def show_sender_requests(mlist, form, sender):
    bysender = helds_by_sender(mlist)
    if not bysender:
        return
    sender_ids = bysender.get(sender)
    if sender_ids is None:
        # BAW: should we print an error message?
        return
    total = len(sender_ids)
    requestsdb = config.db.get_list_requests(mlist)
    for i, id in enumerate(sender_ids):
        key, data = requestsdb.get_record(id)
        show_post_requests(mlist, id, data, total, count + 1, form)



def show_message_requests(mlist, form, id):
    requestdb = config.db.get_list_requests(mlist)
    try:
        id = int(id)
        info = requestdb.get_record(id)
    except (ValueError, KeyError):
        # BAW: print an error message?
        return
    show_post_requests(mlist, id, info, 1, 1, form)



def show_detailed_requests(mlist, form):
    requestsdb = config.db.get_list_requests(mlist)
    total = requestsdb.count_of(RequestType.held_message)
    all = requestsdb.of_type(RequestType.held_message)
    for i, request in enumerate(all):
        key, data = requestdb.get_request(request.id)
        show_post_requests(mlist, request.id, data, total, i + 1, form)



def show_post_requests(mlist, id, info, total, count, form):
    # For backwards compatibility with pre 2.0beta3
    if len(info) == 5:
        ptime, sender, subject, reason, filename = info
        msgdata = {}
    else:
        ptime, sender, subject, reason, filename, msgdata = info
    form.AddItem('<hr>')
    # Header shown on each held posting (including count of total)
    msg = _('Posting Held for Approval')
    if total <> 1:
        msg += _(' (%(count)d of %(total)d)')
    form.AddItem(Center(Header(2, msg)))
    # We need to get the headers and part of the textual body of the message
    # being held.  The best way to do this is to use the email Parser to get
    # an actual object, which will be easier to deal with.  We probably could
    # just do raw reads on the file.
    try:
        msg = readMessage(os.path.join(config.DATA_DIR, filename))
    except IOError, e:
        if e.errno <> errno.ENOENT:
            raise
        form.AddItem(_('<em>Message with id #%(id)d was lost.'))
        form.AddItem('<p>')
        # BAW: kludge to remove id from requests.db.
        try:
            mlist.HandleRequest(id, config.DISCARD)
        except Errors.LostHeldMessage:
            pass
        return
    except email.Errors.MessageParseError:
        form.AddItem(_('<em>Message with id #%(id)d is corrupted.'))
        # BAW: Should we really delete this, or shuttle it off for site admin
        # to look more closely at?
        form.AddItem('<p>')
        # BAW: kludge to remove id from requests.db.
        try:
            mlist.HandleRequest(id, config.DISCARD)
        except Errors.LostHeldMessage:
            pass
        return
    # Get the header text and the message body excerpt
    lines = []
    chars = 0
    # A negative value means, include the entire message regardless of size
    limit = config.ADMINDB_PAGE_TEXT_LIMIT
    for line in email.Iterators.body_line_iterator(msg):
        lines.append(line)
        chars += len(line)
        if chars > limit > 0:
            break
    # Negative values mean display the entire message, regardless of size
    if limit > 0:
        body = EMPTYSTRING.join(lines)[:config.ADMINDB_PAGE_TEXT_LIMIT]
    else:
        body = EMPTYSTRING.join(lines)
    # Get message charset and try encode in list charset
    mcset = msg.get_param('charset', 'us-ascii').lower()
    lcset = Utils.GetCharSet(mlist.preferred_language)
    if mcset <> lcset:
        try:
            body = unicode(body, mcset).encode(lcset)
        except (LookupError, UnicodeError, ValueError):
            pass
    hdrtxt = NL.join(['%s: %s' % (k, v) for k, v in msg.items()])
    hdrtxt = Utils.websafe(hdrtxt)
    # Okay, we've reconstituted the message just fine.  Now for the fun part!
    t = Table(cellspacing=0, cellpadding=0, width='100%')
    t.AddRow([Bold(_('From:')), sender])
    row, col = t.GetCurrentRowIndex(), t.GetCurrentCellIndex()
    t.AddCellInfo(row, col-1, align='right')
    t.AddRow([Bold(_('Subject:')),
              Utils.websafe(Utils.oneline(subject, lcset))])
    t.AddCellInfo(row+1, col-1, align='right')
    t.AddRow([Bold(_('Reason:')), _(reason)])
    t.AddCellInfo(row+2, col-1, align='right')
    when = msgdata.get('received_time')
    if when:
        t.AddRow([Bold(_('Received:')), time.ctime(when)])
        t.AddCellInfo(row+2, col-1, align='right')
    # We can't use a RadioButtonArray here because horizontal placement can be
    # confusing to the user and vertical placement takes up too much
    # real-estate.  This is a hack!
    buttons = Table(cellspacing="5", cellpadding="0")
    buttons.AddRow(map(lambda x, s='&nbsp;'*5: s+x+s,
                       (_('Defer'), _('Approve'), _('Reject'), _('Discard'))))
    buttons.AddRow([Center(RadioButton(id, config.DEFER, 1)),
                    Center(RadioButton(id, config.APPROVE, 0)),
                    Center(RadioButton(id, config.REJECT, 0)),
                    Center(RadioButton(id, config.DISCARD, 0)),
                    ])
    t.AddRow([Bold(_('Action:')), buttons])
    t.AddCellInfo(row+3, col-1, align='right')
    t.AddRow(['&nbsp;',
              CheckBox('preserve-%d' % id, 'on', 0).Format() +
              '&nbsp;' + _('Preserve message for site administrator')
              ])
    t.AddRow(['&nbsp;',
              CheckBox('forward-%d' % id, 'on', 0).Format() +
              '&nbsp;' + _('Additionally, forward this message to: ') +
              TextBox('forward-addr-%d' % id, size=47,
                      value=mlist.GetOwnerEmail()).Format()
              ])
    notice = msgdata.get('rejection_notice', _('[No explanation given]'))
    t.AddRow([
        Bold(_('If you reject this post,<br>please explain (optional):')),
        TextArea('comment-%d' % id, rows=4, cols=EXCERPT_WIDTH,
                 text = Utils.wrap(_(notice), column=80))
        ])
    row, col = t.GetCurrentRowIndex(), t.GetCurrentCellIndex()
    t.AddCellInfo(row, col-1, align='right')
    t.AddRow([Bold(_('Message Headers:')),
              TextArea('headers-%d' % id, hdrtxt,
                       rows=EXCERPT_HEIGHT, cols=EXCERPT_WIDTH, readonly=1)])
    row, col = t.GetCurrentRowIndex(), t.GetCurrentCellIndex()
    t.AddCellInfo(row, col-1, align='right')
    t.AddRow([Bold(_('Message Excerpt:')),
              TextArea('fulltext-%d' % id, Utils.websafe(body),
                       rows=EXCERPT_HEIGHT, cols=EXCERPT_WIDTH, readonly=1)])
    t.AddCellInfo(row+1, col-1, align='right')
    form.AddItem(t)
    form.AddItem('<p>')



def process_form(mlist, doc, cgidata):
    senderactions = {}
    # Sender-centric actions
    for k in cgidata.keys():
        for prefix in ('senderaction-', 'senderpreserve-', 'senderforward-',
                       'senderforwardto-', 'senderfilterp-', 'senderfilter-',
                       'senderclearmodp-', 'senderbanp-'):
            if k.startswith(prefix):
                action = k[:len(prefix)-1]
                sender = unquote_plus(k[len(prefix):])
                value = cgidata.getvalue(k)
                senderactions.setdefault(sender, {})[action] = value
    # discard-all-defers
    try:
        discardalldefersp = cgidata.getvalue('discardalldefersp', 0)
    except ValueError:
        discardalldefersp = 0
    for sender in senderactions.keys():
        actions = senderactions[sender]
        # Handle what to do about all this sender's held messages
        try:
            action = int(actions.get('senderaction', config.DEFER))
        except ValueError:
            action = config.DEFER
        if action == config.DEFER and discardalldefersp:
            action = config.DISCARD
        if action in (config.DEFER, config.APPROVE,
                      config.REJECT, config.DISCARD):
            preserve = actions.get('senderpreserve', 0)
            forward = actions.get('senderforward', 0)
            forwardaddr = actions.get('senderforwardto', '')
            comment = _('No reason given')
            bysender = helds_by_sender(mlist)
            for id in bysender.get(sender, []):
                try:
                    mlist.HandleRequest(id, action, comment, preserve,
                                        forward, forwardaddr)
                except (KeyError, Errors.LostHeldMessage):
                    # That's okay, it just means someone else has already
                    # updated the database while we were staring at the page,
                    # so just ignore it
                    continue
        # Now see if this sender should be added to one of the nonmember
        # sender filters.
        if actions.get('senderfilterp', 0):
            try:
                which = int(actions.get('senderfilter'))
            except ValueError:
                # Bogus form
                which = 'ignore'
            if which == config.ACCEPT:
                mlist.accept_these_nonmembers.append(sender)
            elif which == config.HOLD:
                mlist.hold_these_nonmembers.append(sender)
            elif which == config.REJECT:
                mlist.reject_these_nonmembers.append(sender)
            elif which == config.DISCARD:
                mlist.discard_these_nonmembers.append(sender)
            # Otherwise, it's a bogus form, so ignore it
        # And now see if we're to clear the member's moderation flag.
        if actions.get('senderclearmodp', 0):
            try:
                mlist.setMemberOption(sender, config.Moderate, 0)
            except Errors.NotAMemberError:
                # This person's not a member any more.  Oh well.
                pass
        # And should this address be banned?
        if actions.get('senderbanp', 0):
            if sender not in mlist.ban_list:
                mlist.ban_list.append(sender)
    # Now, do message specific actions
    banaddrs = []
    erroraddrs = []
    for k in cgidata.keys():
        formv = cgidata[k]
        if isinstance(formv, list):
            continue
        try:
            v = int(formv.value)
            request_id = int(k)
        except ValueError:
            continue
        if v not in (config.DEFER, config.APPROVE, config.REJECT,
                     config.DISCARD, config.SUBSCRIBE, config.UNSUBSCRIBE,
                     config.ACCEPT, config.HOLD):
            continue
        # Get the action comment and reasons if present.
        commentkey = 'comment-%d' % request_id
        preservekey = 'preserve-%d' % request_id
        forwardkey = 'forward-%d' % request_id
        forwardaddrkey = 'forward-addr-%d' % request_id
        bankey = 'ban-%d' % request_id
        # Defaults
        comment = _('[No reason given]')
        preserve = 0
        forward = 0
        forwardaddr = ''
        if cgidata.has_key(commentkey):
            comment = cgidata[commentkey].value
        if cgidata.has_key(preservekey):
            preserve = cgidata[preservekey].value
        if cgidata.has_key(forwardkey):
            forward = cgidata[forwardkey].value
        if cgidata.has_key(forwardaddrkey):
            forwardaddr = cgidata[forwardaddrkey].value
        # Should we ban this address?  Do this check before handling the
        # request id because that will evict the record.
        requestsdb = config.db.get_list_requests(mlist)
        if cgidata.getvalue(bankey):
            key, data = requestsdb.get_record(request_id)
            sender = data['sender']
            if sender not in mlist.ban_list:
                mlist.ban_list.append(sender)
        # Handle the request id
        try:
            mlist.HandleRequest(request_id, v, comment,
                                preserve, forward, forwardaddr)
        except (KeyError, Errors.LostHeldMessage):
            # That's okay, it just means someone else has already updated the
            # database while we were staring at the page, so just ignore it
            continue
        except Errors.MMAlreadyAMember, v:
            erroraddrs.append(v)
        except Errors.MembershipIsBanned, pattern:
            data = requestsdb.get_record(request_id)
            sender = data['sender']
            banaddrs.append((sender, pattern))
    # save the list and print the results
    doc.AddItem(Header(2, _('Database Updated...')))
    if erroraddrs:
        for addr in erroraddrs:
            doc.AddItem(`addr` + _(' is already a member') + '<br>')
    if banaddrs:
        for addr, patt in banaddrs:
            doc.AddItem(_('%(addr)s is banned (matched: %(patt)s)') + '<br>')