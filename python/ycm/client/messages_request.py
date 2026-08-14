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

import vim

from ycm.client.base_request import BaseRequest, BuildRequestData
from ycm.vimsupport import PostVimMessage

import logging

_logger = logging.getLogger( __name__ )

# Looooong poll
TIMEOUT_SECONDS = 60

_progress_tokens = {}


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


def _ClearProgress():
  global _progress_tokens
  _progress_tokens.clear()
  vim.command( 'redraw' )
  vim.command( "echo ''" )


def _HandleProgressNotification( progress ):
  global _progress_tokens
  kind = progress.get( 'kind', '' )
  token = str( progress.get( 'token', '' ) )

  if kind == 'begin':
    _progress_tokens[ token ] = progress
    _UpdateProgressEcho()
  elif kind == 'report':
    if token in _progress_tokens:
      _progress_tokens[ token ].update( progress )
      _UpdateProgressEcho()
  elif kind == 'end':
    _progress_tokens.pop( token, None )
    if not _progress_tokens:
      vim.command( 'redraw' )
      vim.command( "echo ''" )


def _UpdateProgressEcho():
  parts = []
  for progress in _progress_tokens.values():
    title = progress.get( 'title', '' )
    message = progress.get( 'message', '' )
    percentage = progress.get( 'percentage' )
    text = title
    if message:
      text = f'{ text }: { message }' if text else message
    if percentage is not None:
      text = f'{ text } ({ int( percentage ) }%)'
    if text:
      parts.append( text )
  if parts:
    msg = '[ycm: ' + ' | '.join( parts ) + ']'
    vim.command( 'redraw' )
    vim.command( "echo '" + msg.replace( "'", "''" ) + "'" )
