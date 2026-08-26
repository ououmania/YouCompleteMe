# Copyright (C) 2017 YouCompleteMe contributors
#
# This file is part of YouCompleteMe.
#
# YouCompleteMe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# YouCompleteMe is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with YouCompleteMe.  If not, see <http://www.gnu.org/licenses/>.

import json
import logging
import time

import vim

from ycm.client.base_request import BaseRequest, BuildRequestData
from ycm.vimsupport import ( GetIntValue,
                             PostVimMessage,
                             VimSupportsPopupWindows )

_logger = logging.getLogger( __name__ )

# Looooong poll
TIMEOUT_SECONDS = 60

PROGRESS_POPUP = 'popup'
PROGRESS_STATUSLINE = 'statusline'
PROGRESS_NONE = 'none'

# 两次 popup 刷新的最短间隔（秒），避免 report 刷屏
PROGRESS_UPDATE_MIN_INTERVAL = 0.1

_progress_tokens = {}
_progress_popup_id = None
_last_progress_summary = None
_last_progress_update = 0.0

_PROGRESS_POPUP_OPTIONS = {
  'line': -1,
  'col': 1,
  'pos': 'botleft',
  'wrap': 0,
  'fixed': 1,
  'flip': 1,
}


class MessagesPoll( BaseRequest ):
  def __init__( self, buff ):
    super( MessagesPoll, self ).__init__()
    self._request_data = BuildRequestData( buff.number )
    self._response_future = None


  def _SendRequest( self ):
    self._response_future = self.PostDataToHandlerAsync(
      self._request_data,
      'receive_messages',
      timeout = TIMEOUT_SECONDS )
    return


  def Poll( self, diagnostics_handler ):
    """This should be called regularly to check for new messages in this buffer.
    Returns True if Poll should be called again in a while. Returns False when
    the completer or server indicated that further polling should not be done
    for the requested file."""

    if self._response_future is None:
      # First poll
      self._SendRequest()
      return True

    if not self._response_future.done():
      # Nothing yet...
      return True

    response = self.HandleFuture( self._response_future,
                                  display_message = False )
    if response is None:
      # Server returned an exception.
      _ClearProgress()
      return False

    poll_again = _HandlePollResponse( response, diagnostics_handler )
    if poll_again:
      self._SendRequest()
      return True

    return False


def _HandlePollResponse( response, diagnostics_handler ):
  if isinstance( response, list ):
    for notification in response:
      if 'message' in notification:
        PostVimMessage( notification[ 'message' ],
                        warning = False,
                        truncate = True )
      elif 'diagnostics' in notification:
        diagnostics_handler.UpdateWithNewDiagnosticsForFile(
          notification[ 'filepath' ],
          notification[ 'diagnostics' ] )
      elif 'lsp_progress' in notification:
        _HandleProgressNotification( notification[ 'lsp_progress' ] )
  elif response is False:
    # Don't keep polling for this file; clear any pending progress display
    _ClearProgress()
    return False
  # else any truthy response means "nothing to see here; poll again in a
  # while"

  # Start the next poll (only if the last poll didn't raise an exception)
  return True


def GetProgressSummary():
  """Returns a summary of active LSP progress, or None if idle."""
  if not _progress_tokens:
    return None
  parts = []
  for p in _progress_tokens.values():
    title = p.get( 'title', '' )
    message = p.get( 'message', '' )
    percentage = p.get( 'percentage' )
    text = title
    if message:
      text = f'{ text }: { message }' if text else message
    if percentage is not None:
      text = f'{ text } ({ int( percentage ) }%)'
    if text:
      parts.append( text )
  return ' | '.join( parts ) if parts else None


def GetLspProgress():
  """Returns a summary of active LSP progress for use in the statusline, or an
  empty string if idle."""
  summary = GetProgressSummary()
  return summary if summary is not None else ''


def _ClearProgress():
  _progress_tokens.clear()
  _CloseProgressPopup()


def _HandleProgressNotification( progress ):
  kind = progress.get( 'kind', '' )
  token = str( progress.get( 'token', '' ) )

  if kind == 'begin':
    _progress_tokens[ token ] = progress
  elif kind == 'report':
    if token in _progress_tokens:
      _progress_tokens[ token ].update( progress )
  elif kind == 'end':
    _progress_tokens.pop( token, None )

  _UpdateProgressDisplay()


def _GetProgressDisplayMode():
  mode = vim.vars.get( 'ycm_show_lsp_progress', PROGRESS_POPUP )
  if mode in ( PROGRESS_POPUP, PROGRESS_STATUSLINE ):
    return mode
  return PROGRESS_NONE


def _UpdateProgressDisplay():
  if _GetProgressDisplayMode() != PROGRESS_POPUP:
    return
  if not VimSupportsPopupWindows():
    return
  _UpdateProgressPopup( GetProgressSummary() )


def _UpdateProgressPopup( summary ):
  global _progress_popup_id, _last_progress_summary, _last_progress_update

  if summary is None:
    _CloseProgressPopup()
    return

  if summary == _last_progress_summary and _progress_popup_id is not None:
    return

  now = time.time()
  if ( _progress_popup_id is not None and
       now - _last_progress_update < PROGRESS_UPDATE_MIN_INTERVAL ):
    return

  lines = [ summary ]
  if _progress_popup_id is None:
    _progress_popup_id = GetIntValue(
      f'popup_create( { json.dumps( lines ) }, '
      f'{ json.dumps( _PROGRESS_POPUP_OPTIONS ) } )' )
  else:
    vim.eval( f'popup_settext( { _progress_popup_id }, '
              f'{ json.dumps( lines ) } )' )

  _last_progress_summary = summary
  _last_progress_update = now


def _CloseProgressPopup():
  global _progress_popup_id, _last_progress_summary
  if _progress_popup_id is not None:
    vim.eval( f'popup_close( { _progress_popup_id } )' )
    _progress_popup_id = None
  _last_progress_summary = None
