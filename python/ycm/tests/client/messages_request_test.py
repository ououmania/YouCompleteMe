# Copyright (C) 2017 YouCompleteMe Contributors
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

from ycm.tests.test_utils import MockVimModule
MockVimModule()

from hamcrest import assert_that, contains_string, equal_to
from unittest import TestCase
from unittest.mock import patch, call

from ycm.client.messages_request import ( _ClearProgress,
                                          _HandleProgressNotification,
                                          _HandlePollResponse,
                                          GetLspProgress,
                                          GetProgressSummary )
from ycm.tests.test_utils import ExtendedMock


class MessagesRequestTest( TestCase ):
  def setUp( self ):
    _ClearProgress()


  def tearDown( self ):
    _ClearProgress()


  def test_ProgressNotification_BeginReportEnd( self ):
    _HandleProgressNotification( {
      'kind': 'begin',
      'token': 'rust-analyzer/roots',
      'title': 'Indexing',
    } )
    assert_that( GetProgressSummary(), equal_to( 'Indexing' ) )

    _HandleProgressNotification( {
      'kind': 'report',
      'token': 'rust-analyzer/roots',
      'message': 'foo',
      'percentage': 50,
    } )
    assert_that( GetProgressSummary(), equal_to( 'Indexing: foo (50%)' ) )

    _HandleProgressNotification( {
      'kind': 'end',
      'token': 'rust-analyzer/roots',
    } )
    assert_that( GetProgressSummary(), equal_to( None ) )


  def test_ProgressNotification_MultipleTokens( self ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 'a',
                                   'title': 'A' } )
    _HandleProgressNotification( { 'kind': 'begin', 'token': 'b',
                                   'title': 'B' } )
    assert_that( GetProgressSummary(), equal_to( 'A | B' ) )

    _HandleProgressNotification( { 'kind': 'end', 'token': 'a' } )
    assert_that( GetProgressSummary(), equal_to( 'B' ) )


  def test_GetLspProgress_Idle( self ):
    assert_that( GetLspProgress(), equal_to( '' ) )


  def test_GetLspProgress_Active( self ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 't',
                                   'title': 'X' } )
    assert_that( GetLspProgress(), equal_to( 'X' ) )


  @patch( 'ycm.client.messages_request._GetProgressDisplayMode',
          return_value = 'popup' )
  @patch( 'ycm.client.messages_request.VimSupportsPopupWindows',
          return_value = True )
  @patch( 'ycm.client.messages_request.GetIntValue', return_value = 7 )
  @patch( 'ycm.client.messages_request.time.time',
          side_effect = [ 0.0, 0.20 ] )
  @patch( 'ycm.client.messages_request.vim.eval' )
  def test_ProgressNotification_PopupLifecycle( self, vim_eval, time_time,
                                                get_int_value,
                                                vim_supports_popup,
                                                progress_mode ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 't',
                                   'title': 'Indexing' } )
    get_int_value.assert_called_once()
    vim_eval.assert_not_called()

    create_args = get_int_value.call_args[ 0 ][ 0 ]
    assert_that( create_args,
                 contains_string( '"line": -1' ) )
    assert_that( create_args,
                 contains_string( '"col": 1' ) )
    assert_that( create_args,
                 contains_string( '"pos": "botleft"' ) )

    _HandleProgressNotification( { 'kind': 'report', 'token': 't',
                                   'message': 'foo', 'percentage': 50 } )
    vim_eval.assert_called_once_with(
      'popup_settext( 7, ["Indexing: foo (50%)"] )' )

    _HandleProgressNotification( { 'kind': 'end', 'token': 't' } )
    vim_eval.assert_called_with( 'popup_close( 7 )' )


  @patch( 'ycm.client.messages_request._GetProgressDisplayMode',
          return_value = 'popup' )
  @patch( 'ycm.client.messages_request.VimSupportsPopupWindows',
          return_value = True )
  @patch( 'ycm.client.messages_request.GetIntValue', return_value = 7 )
  @patch( 'ycm.client.messages_request.time.time',
          side_effect = [ 0.0, 0.05, 0.20 ] )
  @patch( 'ycm.client.messages_request.vim.eval' )
  def test_ProgressNotification_PopupThrottle( self, vim_eval, time_time,
                                               get_int_value,
                                               vim_supports_popup,
                                               progress_mode ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 't',
                                   'title': 'Indexing' } )

    # 距上次刷新仅 0.05s，低于 0.1s 阈值，应被节流跳过（不 settext）
    _HandleProgressNotification( { 'kind': 'report', 'token': 't',
                                   'message': 'early' } )
    vim_eval.assert_not_called()

    # 距上次刷新 0.20s，超过阈值，应刷新
    _HandleProgressNotification( { 'kind': 'report', 'token': 't',
                                   'message': 'late' } )
    vim_eval.assert_called_once_with( 'popup_settext( 7, ["Indexing: late"] )' )

    # 结束，关闭浮窗，清理 popup 状态，避免泄漏到 tearDown
    _HandleProgressNotification( { 'kind': 'end', 'token': 't' } )
    vim_eval.assert_called_with( 'popup_close( 7 )' )


  def test_HandlePollResponse_NoMessages( self ):
    assert_that( _HandlePollResponse( True, None ), equal_to( True ) )

    # Other non-False responses mean the same thing
    assert_that( _HandlePollResponse( '', None ), equal_to( True ) )
    assert_that( _HandlePollResponse( 1, None ), equal_to( True ) )
    assert_that( _HandlePollResponse( {}, None ), equal_to( True ) )


  def test_HandlePollResponse_PollingNotSupported( self ):
    assert_that( _HandlePollResponse( False, None ), equal_to( False ) )

    # 0 is not False
    assert_that( _HandlePollResponse( 0, None ), equal_to( True ) )


  @patch( 'ycm.client.messages_request.PostVimMessage',
          new_callable = ExtendedMock )
  def test_HandlePollResponse_SingleMessage( self, post_vim_message ):
    assert_that( _HandlePollResponse( [ { 'message': 'this is a message' } ] ,
                                      None ),
                 equal_to( True ) )

    post_vim_message.assert_has_exact_calls( [
      call( 'this is a message', warning=False, truncate=True )
    ] )


  @patch( 'ycm.client.messages_request.PostVimMessage',
          new_callable = ExtendedMock )
  def test_HandlePollResponse_MultipleMessages( self, post_vim_message ):
    assert_that( _HandlePollResponse( [ { 'message': 'this is a message' },
                                        { 'message': 'this is another one' } ] ,
                                      None ),
                 equal_to( True ) )

    post_vim_message.assert_has_exact_calls( [
      call( 'this is a message', warning=False, truncate=True ),
      call( 'this is another one', warning=False, truncate=True )
    ] )


  def test_HandlePollResponse_SingleDiagnostic( self ):
    diagnostics_handler = ExtendedMock()
    messages = [
      { 'filepath': 'foo', 'diagnostics': [ 'PLACEHOLDER' ] },
    ]
    assert_that( _HandlePollResponse( messages, diagnostics_handler ),
                 equal_to( True ) )
    diagnostics_handler.UpdateWithNewDiagnosticsForFile.assert_has_exact_calls(
      [
        call( 'foo', [ 'PLACEHOLDER' ] )
      ] )


  def test_HandlePollResponse_MultipleDiagnostics( self ):
    diagnostics_handler = ExtendedMock()
    messages = [
      { 'filepath': 'foo', 'diagnostics': [ 'PLACEHOLDER1' ] },
      { 'filepath': 'bar', 'diagnostics': [ 'PLACEHOLDER2' ] },
      { 'filepath': 'baz', 'diagnostics': [ 'PLACEHOLDER3' ] },
      { 'filepath': 'foo', 'diagnostics': [ 'PLACEHOLDER4' ] },
    ]
    assert_that( _HandlePollResponse( messages, diagnostics_handler ),
                 equal_to( True ) )
    diagnostics_handler.UpdateWithNewDiagnosticsForFile.assert_has_exact_calls(
      [
        call( 'foo', [ 'PLACEHOLDER1' ] ),
        call( 'bar', [ 'PLACEHOLDER2' ] ),
        call( 'baz', [ 'PLACEHOLDER3' ] ),
        call( 'foo', [ 'PLACEHOLDER4' ] )
      ] )


  @patch( 'ycm.client.messages_request.PostVimMessage',
          new_callable = ExtendedMock )
  def test_HandlePollResponse_MultipleMessagesAndDiagnostics(
      self, post_vim_message ):
    diagnostics_handler = ExtendedMock()
    messages = [
      { 'filepath': 'foo', 'diagnostics': [ 'PLACEHOLDER1' ] },
      { 'message': 'On the first day of Christmas, my VimScript gave to me' },
      { 'filepath': 'bar', 'diagnostics': [ 'PLACEHOLDER2' ] },
      { 'message': 'A test file in a Command-T' },
      { 'filepath': 'baz', 'diagnostics': [ 'PLACEHOLDER3' ] },
      { 'message': 'On the second day of Christmas, my VimScript gave to me' },
      { 'filepath': 'foo', 'diagnostics': [ 'PLACEHOLDER4' ] },
      { 'message': 'Two popup menus, and a test file in a Command-T' },
    ]
    assert_that( _HandlePollResponse( messages, diagnostics_handler ),
                 equal_to( True ) )
    diagnostics_handler.UpdateWithNewDiagnosticsForFile.assert_has_exact_calls(
      [
        call( 'foo', [ 'PLACEHOLDER1' ] ),
        call( 'bar', [ 'PLACEHOLDER2' ] ),
        call( 'baz', [ 'PLACEHOLDER3' ] ),
        call( 'foo', [ 'PLACEHOLDER4' ] )
      ] )

    post_vim_message.assert_has_exact_calls( [
      call( 'On the first day of Christmas, my VimScript gave to me',
            warning=False,
            truncate=True ),
      call( 'A test file in a Command-T', warning=False, truncate=True ),
      call( 'On the second day of Christmas, my VimScript gave to me',
            warning=False,
            truncate=True ),
      call( 'Two popup menus, and a test file in a Command-T',
            warning=False,
            truncate=True ),
    ] )
