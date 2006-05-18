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

"""A `safe' dictionary for string interpolation."""

COMMASPACE = ', '

# XXX This module should go away.



class SafeDict(dict):
    """Dictionary which returns a default value for unknown keys.

    This is used in maketext so that editing templates is a bit more robust.
    """
    def __getitem__(self, key):
        try:
            return super(SafeDict, self).__getitem__(key)
        except KeyError:
            if isinstance(key, basestring):
                return '%('+key+')s'
            else:
                return '<Missing key: %s>' % `key`

    def interpolate(self, template):
        return template % self



class MsgSafeDict(SafeDict):
    def __init__(self, msg, d=None):
        self.__msg = msg
        if d is None:
            d = {}
        super(MsgSafeDict, self).__init__(d)

    def __getitem__(self, key):
        if key.startswith('msg_'):
            return self.__msg.get(key[4:], 'n/a')
        elif key.startswith('allmsg_'):
            missing = []
            all = self.__msg.get_all(key[7:], missing)
            if all is missing:
                return 'n/a'
            return COMMASPACE.join(all)
        else:
            return super(MsgSafeDict, self).__getitem__(key)

    def copy(self):
        d = super(MsgSafeDict, self).copy()
        for k in self.__msg.keys():
            vals = self.__msg.get_all(k)
            if len(vals) == 1:
                d['msg_'+k.lower()] = vals[0]
            else:
                d['allmsg_'+k.lower()] = COMMASPACE.join(vals)
        return d
