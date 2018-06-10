import getpass
import hashlib
import logging
import sys
import warnings

from ..tl.functions.help import AcceptTermsOfServiceRequest
from ..tl.functions.updates import GetDifferenceRequest
from ..tl.types.updates import (
    DifferenceSlice, DifferenceEmpty, Difference, DifferenceTooLong
)

try:
    import socks
except ImportError:
    socks = None


from .telegrambaseclient import TelegramBaseClient
from .. import helpers, events
from ..errors import (
    PhoneCodeEmptyError, PhoneCodeExpiredError,
    PhoneCodeHashEmptyError, PhoneCodeInvalidError, SessionPasswordNeededError,
    PhoneNumberUnoccupiedError,
    PhoneNumberOccupiedError
)
from ..tl.functions.account import (
    GetPasswordRequest, UpdatePasswordSettingsRequest
)
from ..tl.functions.auth import (
    CheckPasswordRequest, LogOutRequest, SendCodeRequest, SignInRequest,
    SignUpRequest, ResendCodeRequest, ImportBotAuthorizationRequest
)

from ..tl.types import (
    UpdateNewMessage, Updates
)
from ..tl.types.account import PasswordInputSettings, NoPassword

__log__ = logging.getLogger(__name__)
import os
from .. import utils
from ..errors import RPCError


class TelegramClient(TelegramBaseClient):
    """
    Initializes the Telegram client with the specified API ID and Hash. This
    is identical to the `telethon.telegram_bare_client.TelegramBareClient`
    but it contains "friendly methods", so please refer to its documentation
    to know what parameters you can use when creating a new instance.
    """

    # region Telegram requests functions

    # region Authorization requests

    def send_code_request(self, phone, force_sms=False):
        """
        Sends a code request to the specified phone number.

        Args:
            phone (`str` | `int`):
                The phone to which the code will be sent.

            force_sms (`bool`, optional):
                Whether to force sending as SMS.

        Returns:
            An instance of :tl:`SentCode`.
        """
        phone = utils.parse_phone(phone) or self._phone
        phone_hash = self._phone_code_hash.get(phone)

        if not phone_hash:
            result = self(SendCodeRequest(phone, self.api_id, self.api_hash))
            self._tos = result.terms_of_service
            self._phone_code_hash[phone] = phone_hash = result.phone_code_hash
        else:
            force_sms = True

        self._phone = phone

        if force_sms:
            result = self(ResendCodeRequest(phone, phone_hash))
            self._phone_code_hash[phone] = result.phone_code_hash

        return result

    def start(self,
              phone=lambda: input('Please enter your phone: '),
              password=lambda: getpass.getpass('Please enter your password: '),
              bot_token=None, force_sms=False, code_callback=None,
              first_name='New User', last_name=''):
        """
        Convenience method to interactively connect and sign in if required,
        also taking into consideration that 2FA may be enabled in the account.

        If the phone doesn't belong to an existing account (and will hence
        `sign_up` for a new one),  **you are agreeing to Telegram's
        Terms of Service. This is required and your account
        will be banned otherwise.** See https://telegram.org/tos
        and https://core.telegram.org/api/terms.

        Example usage:
            >>> client = TelegramClient(session, api_id, api_hash).start(phone)
            Please enter the code you received: 12345
            Please enter your password: *******
            (You are now logged in)

        Args:
            phone (`str` | `int` | `callable`):
                The phone (or callable without arguments to get it)
                to which the code will be sent.

            password (`callable`, optional):
                The password for 2 Factor Authentication (2FA).
                This is only required if it is enabled in your account.

            bot_token (`str`):
                Bot Token obtained by `@BotFather <https://t.me/BotFather>`_
                to log in as a bot. Cannot be specified with ``phone`` (only
                one of either allowed).

            force_sms (`bool`, optional):
                Whether to force sending the code request as SMS.
                This only makes sense when signing in with a `phone`.

            code_callback (`callable`, optional):
                A callable that will be used to retrieve the Telegram
                login code. Defaults to `input()`.

            first_name (`str`, optional):
                The first name to be used if signing up. This has no
                effect if the account already exists and you sign in.

            last_name (`str`, optional):
                Similar to the first name, but for the last. Optional.

        Returns:
            This `TelegramClient`, so initialization
            can be chained with ``.start()``.
        """

        if code_callback is None:
            def code_callback():
                return input('Please enter the code you received: ')
        elif not callable(code_callback):
            raise ValueError(
                'The code_callback parameter needs to be a callable '
                'function that returns the code you received by Telegram.'
            )

        if not phone and not bot_token:
            raise ValueError('No phone number or bot token provided.')

        if phone and bot_token and not callable(phone):
            raise ValueError('Both a phone and a bot token provided, '
                             'must only provide one of either')

        if not self.is_connected():
            self.connect()

        if self.is_user_authorized():
            self._check_events_pending_resolve()
            return self

        if bot_token:
            self.sign_in(bot_token=bot_token)
            return self

        # Turn the callable into a valid phone number
        while callable(phone):
            phone = utils.parse_phone(phone()) or phone

        me = None
        attempts = 0
        max_attempts = 3
        two_step_detected = False

        sent_code = self.send_code_request(phone, force_sms=force_sms)
        sign_up = not sent_code.phone_registered
        while attempts < max_attempts:
            try:
                if sign_up:
                    me = self.sign_up(code_callback(), first_name, last_name)
                else:
                    # Raises SessionPasswordNeededError if 2FA enabled
                    me = self.sign_in(phone, code_callback())
                break
            except SessionPasswordNeededError:
                two_step_detected = True
                break
            except PhoneNumberOccupiedError:
                sign_up = False
            except PhoneNumberUnoccupiedError:
                sign_up = True
            except (PhoneCodeEmptyError, PhoneCodeExpiredError,
                    PhoneCodeHashEmptyError, PhoneCodeInvalidError):
                print('Invalid code. Please try again.', file=sys.stderr)

            attempts += 1
        else:
            raise RuntimeError(
                '{} consecutive sign-in attempts failed. Aborting'
                .format(max_attempts)
            )

        if two_step_detected:
            if not password:
                raise ValueError(
                    "Two-step verification is enabled for this account. "
                    "Please provide the 'password' argument to 'start()'."
                )
            # TODO If callable given make it retry on invalid
            if callable(password):
                password = password()
            me = self.sign_in(phone=phone, password=password)

        # We won't reach here if any step failed (exit by exception)
        signed, name = 'Signed in successfully as', utils.get_display_name(me)
        try:
            print(signed, name)
        except UnicodeEncodeError:
            # Some terminals don't support certain characters
            print(signed, name.encode('utf-8', errors='ignore')
                              .decode('ascii', errors='ignore'))

        self._check_events_pending_resolve()
        return self

    def sign_in(self, phone=None, code=None,
                password=None, bot_token=None, phone_code_hash=None):
        """
        Starts or completes the sign in process with the given phone number
        or code that Telegram sent.

        Args:
            phone (`str` | `int`):
                The phone to send the code to if no code was provided,
                or to override the phone that was previously used with
                these requests.

            code (`str` | `int`):
                The code that Telegram sent. Note that if you have sent this
                code through the application itself it will immediately
                expire. If you want to send the code, obfuscate it somehow.
                If you're not doing any of this you can ignore this note.

            password (`str`):
                2FA password, should be used if a previous call raised
                SessionPasswordNeededError.

            bot_token (`str`):
                Used to sign in as a bot. Not all requests will be available.
                This should be the hash the @BotFather gave you.

            phone_code_hash (`str`):
                The hash returned by .send_code_request. This can be set to None
                to use the last hash known.

        Returns:
            The signed in user, or the information about
            :meth:`send_code_request`.
        """
        if self.is_user_authorized():
            self._check_events_pending_resolve()
            return self.get_me()

        if phone and not code and not password:
            return self.send_code_request(phone)
        elif code:
            phone = utils.parse_phone(phone) or self._phone
            phone_code_hash = \
                phone_code_hash or self._phone_code_hash.get(phone, None)

            if not phone:
                raise ValueError(
                    'Please make sure to call send_code_request first.'
                )
            if not phone_code_hash:
                raise ValueError('You also need to provide a phone_code_hash.')

            # May raise PhoneCodeEmptyError, PhoneCodeExpiredError,
            # PhoneCodeHashEmptyError or PhoneCodeInvalidError.
            result = self(SignInRequest(phone, phone_code_hash, str(code)))
        elif password:
            salt = self(GetPasswordRequest()).current_salt
            result = self(CheckPasswordRequest(
                helpers.get_password_hash(password, salt)
            ))
        elif bot_token:
            result = self(ImportBotAuthorizationRequest(
                flags=0, bot_auth_token=bot_token,
                api_id=self.api_id, api_hash=self.api_hash
            ))
        else:
            raise ValueError(
                'You must provide a phone and a code the first time, '
                'and a password only if an RPCError was raised before.'
            )

        self._self_input_peer = utils.get_input_peer(
            result.user, allow_self=False
        )
        self._set_connected_and_authorized()
        return result.user

    def sign_up(self, code, first_name, last_name=''):
        """
        Signs up to Telegram if you don't have an account yet.
        You must call .send_code_request(phone) first.

        **By using this method you're agreeing to Telegram's
        Terms of Service. This is required and your account
        will be banned otherwise.** See https://telegram.org/tos
        and https://core.telegram.org/api/terms.

        Args:
            code (`str` | `int`):
                The code sent by Telegram

            first_name (`str`):
                The first name to be used by the new account.

            last_name (`str`, optional)
                Optional last name.

        Returns:
            The new created :tl:`User`.
        """
        if self.is_user_authorized():
            self._check_events_pending_resolve()
            return self.get_me()

        if self._tos and self._tos.text:
            if self.parse_mode:
                t = self.parse_mode.unparse(self._tos.text, self._tos.entities)
            else:
                t = self._tos.text
            sys.stderr.write("{}\n".format(t))
            sys.stderr.flush()

        result = self(SignUpRequest(
            phone_number=self._phone,
            phone_code_hash=self._phone_code_hash.get(self._phone, ''),
            phone_code=str(code),
            first_name=first_name,
            last_name=last_name
        ))

        if self._tos:
            self(AcceptTermsOfServiceRequest(self._tos.id))

        self._self_input_peer = utils.get_input_peer(
            result.user, allow_self=False
        )
        self._set_connected_and_authorized()
        return result.user

    def log_out(self):
        """
        Logs out Telegram and deletes the current ``*.session`` file.

        Returns:
            ``True`` if the operation was successful.
        """
        try:
            self(LogOutRequest())
        except RPCError:
            return False

        self.disconnect()
        self.session.delete()
        self._authorized = False
        return True

    # endregion

    # region Downloading media requests

    # endregion

    # endregion

    # region Event handling

    def on(self, event):
        """
        Decorator helper method around add_event_handler().

        Args:
            event (`_EventBuilder` | `type`):
                The event builder class or instance to be used,
                for instance ``events.NewMessage``.
        """
        def decorator(f):
            self.add_event_handler(f, event)
            return f

        return decorator

    def _check_events_pending_resolve(self):
        if self._events_pending_resolve:
            for event in self._events_pending_resolve:
                event.resolve(self)
            self._events_pending_resolve.clear()

    def _on_handler(self, update):
        for builder, callback in self._event_builders:
            event = builder.build(update)
            if event:
                if hasattr(event, '_set_client'):
                    event._set_client(self)
                else:
                    event._client = self

                event.original_update = update
                try:
                    callback(event)
                except events.StopPropagation:
                    __log__.debug(
                        "Event handler '{}' stopped chain of "
                        "propagation for event {}."
                        .format(callback.__name__, type(event).__name__)
                    )
                    break

    def add_event_handler(self, callback, event=None):
        """
        Registers the given callback to be called on the specified event.

        Args:
            callback (`callable`):
                The callable function accepting one parameter to be used.

            event (`_EventBuilder` | `type`, optional):
                The event builder class or instance to be used,
                for instance ``events.NewMessage``.

                If left unspecified, `telethon.events.raw.Raw` (the
                :tl:`Update` objects with no further processing) will
                be passed instead.
        """
        if self.updates.workers is None:
            warnings.warn(
                "You have not setup any workers, so you won't receive updates."
                " Pass update_workers=1 when creating the TelegramClient,"
                " or set client.self.updates.workers = 1"
            )

        self.updates.handler = self._on_handler
        if isinstance(event, type):
            event = event()
        elif not event:
            event = events.Raw()

        if self.is_user_authorized():
            event.resolve(self)
            self._check_events_pending_resolve()
        else:
            self._events_pending_resolve.append(event)

        self._event_builders.append((event, callback))

    def remove_event_handler(self, callback, event=None):
        """
        Inverse operation of :meth:`add_event_handler`.

        If no event is given, all events for this callback are removed.
        Returns how many callbacks were removed.
        """
        found = 0
        if event and not isinstance(event, type):
            event = type(event)

        i = len(self._event_builders)
        while i:
            i -= 1
            ev, cb = self._event_builders[i]
            if cb == callback and (not event or isinstance(ev, event)):
                del self._event_builders[i]
                found += 1

        return found

    def list_event_handlers(self):
        """
        Lists all added event handlers, returning a list of pairs
        consisting of (callback, event).
        """
        return [(callback, event) for event, callback in self._event_builders]

    def add_update_handler(self, handler):
        """Deprecated, see :meth:`add_event_handler`."""
        warnings.warn(
            'add_update_handler is deprecated, use the @client.on syntax '
            'or add_event_handler(callback, events.Raw) instead (see '
            'https://telethon.rtfd.io/en/latest/extra/basic/working-'
            'with-updates.html)'
        )
        return self.add_event_handler(handler, events.Raw)

    def remove_update_handler(self, handler):
        return self.remove_event_handler(handler)

    def list_update_handlers(self):
        return [callback for callback, _ in self.list_event_handlers()]

    def catch_up(self):
        state = self.session.get_update_state(0)
        if not state or not state.pts:
            return

        self.session.catching_up = True
        try:
            while True:
                d = self(GetDifferenceRequest(state.pts, state.date, state.qts))
                if isinstance(d, DifferenceEmpty):
                    state.date = d.date
                    state.seq = d.seq
                    break
                elif isinstance(d, (DifferenceSlice, Difference)):
                    if isinstance(d, Difference):
                        state = d.state
                    elif d.intermediate_state.pts > state.pts:
                        state = d.intermediate_state
                    else:
                        # TODO Figure out why other applications can rely on
                        # using always the intermediate_state to eventually
                        # reach a DifferenceEmpty, but that leads to an
                        # infinite loop here (so check against old pts to stop)
                        break

                    self.updates.process(Updates(
                        users=d.users,
                        chats=d.chats,
                        date=state.date,
                        seq=state.seq,
                        updates=d.other_updates + [UpdateNewMessage(m, 0, 0)
                                                   for m in d.new_messages]
                    ))
                elif isinstance(d, DifferenceTooLong):
                    break
        finally:
            self.session.set_update_state(0, state)
            self.session.catching_up = False

    # endregion

    # region Small utilities to make users' life easier

    def _set_connected_and_authorized(self):
        super()._set_connected_and_authorized()
        self._check_events_pending_resolve()

    def edit_2fa(self, current_password=None, new_password=None, hint='',
                 email=None):
        """
        Changes the 2FA settings of the logged in user, according to the
        passed parameters. Take note of the parameter explanations.

        Has no effect if both current and new password are omitted.

        current_password (`str`, optional):
            The current password, to authorize changing to ``new_password``.
            Must be set if changing existing 2FA settings.
            Must **not** be set if 2FA is currently disabled.
            Passing this by itself will remove 2FA (if correct).

        new_password (`str`, optional):
            The password to set as 2FA.
            If 2FA was already enabled, ``current_password`` **must** be set.
            Leaving this blank or ``None`` will remove the password.

        hint (`str`, optional):
            Hint to be displayed by Telegram when it asks for 2FA.
            Leaving unspecified is highly discouraged.
            Has no effect if ``new_password`` is not set.

        email (`str`, optional):
            Recovery and verification email. Raises ``EmailUnconfirmedError``
            if value differs from current one, and has no effect if
            ``new_password`` is not set.

        Returns:
            ``True`` if successful, ``False`` otherwise.
        """
        if new_password is None and current_password is None:
            return False

        pass_result = self(GetPasswordRequest())
        if isinstance(pass_result, NoPassword) and current_password:
            current_password = None

        salt_random = os.urandom(8)
        salt = pass_result.new_salt + salt_random
        if not current_password:
            current_password_hash = salt
        else:
            current_password = pass_result.current_salt +\
                current_password.encode() + pass_result.current_salt
            current_password_hash = hashlib.sha256(current_password).digest()

        if new_password:  # Setting new password
            new_password = salt + new_password.encode('utf-8') + salt
            new_password_hash = hashlib.sha256(new_password).digest()
            new_settings = PasswordInputSettings(
                new_salt=salt,
                new_password_hash=new_password_hash,
                hint=hint
            )
            if email:  # If enabling 2FA or changing email
                new_settings.email = email  # TG counts empty string as None
            return self(UpdatePasswordSettingsRequest(
                current_password_hash, new_settings=new_settings
            ))
        else:  # Removing existing password
            return self(UpdatePasswordSettingsRequest(
                current_password_hash,
                new_settings=PasswordInputSettings(
                    new_salt=bytes(),
                    new_password_hash=bytes(),
                    hint=hint
                )
            ))

    # endregion
