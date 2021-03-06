from authors.apps.profiles.models import Profile
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import serializers, generics
from rest_framework.reverse import reverse
from django.utils.encoding import force_text
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth import get_user_model
from django.conf import settings
from rest_framework.views import APIView
from datetime import datetime, timedelta
import jwt
import re
import facebook
from decouple import config
import oauth2
import json
import urllib.request
from authors.apps.core.utils.user_management import (
    get_id_from_token,
)
from rest_framework import permissions
from rest_framework.exceptions import AuthenticationFailed

from .renderers import UserJSONRenderer
from .serializers import (
    LoginSerializer, RegistrationSerializer,
    UpdatePasswordSerializer,
    SendPasswordResetEmailSerializer, UnsubscribeNotificationsSerializer
)
from .send_email_util import SendEmail

User = get_user_model()


def get_data_pipeline(backend, response, *args, **kwargs):  # pragma: no cover
    if backend.name == 'google-oauth2':  # pragma: no cover
        email = kwargs['details']['email']
        username = response['displayName']
    if backend.name == 'facebook':  # pragma: no cover
        email = kwargs['details']['email']
        username = response.get('name')
    if backend.name == 'twitter':  # pragma: no cover
        email = kwargs['details']['email']
        username = response['screen_name']
    try:  # pragma: no cover
        global auth_user
        auth_user = User.objects.get(email=email)
    except(TypeError, ValueError, OverflowError, User.DoesNotExist):
        auth_user = None
    RegisterReturnUser(auth_user, username, email)


class RegistrationAPIView(generics.CreateAPIView):
    """post: Register a user."""
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = RegistrationSerializer

    def post(self, request):
        user = request.data.get('user', {})
        # The create serializer, validate serializer, save serializer pattern
        # below is common and you will see it a lot throughout this course and
        # your own work later on. Get familiar with it.
        serializer = self.serializer_class(data=user)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        user = User.objects.get(email=user["email"])
        uid = force_text(urlsafe_base64_encode(user.email.encode("utf8")))
        activation_token = default_token_generator.make_token(user)
        self.email = user.email

        self.mail_subject = "Activate your Authors Haven account."
        self.message = """
            Hi {},
            Please click on the link to confirm your registration,
            {}://{}/api/users/activate/{}/{}""".format(user.username,
                                                       request.scheme,
                                                       request.get_host(),
                                                       uid,
                                                       activation_token)
        SendEmail.send_email(self, self.mail_subject, self.message, self.email)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LoginAPIView(generics.CreateAPIView):
    """post: Login a user."""
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = LoginSerializer

    def post(self, request):
        user = request.data.get('user', {})
        # Notice here that we do not call `serializer.save()` like we did for
        # the registration endpoint. This is because we don't actually have
        # anything to save. Instead, the `validate` method on our serializer
        # handles everything we need.
        serializer = self.serializer_class(data=user)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AccountVerificationAPIView(APIView):
    """get: User account verifications."""
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)

    def get(self, request, uidb64, activation_token):
        try:
            email = force_text(urlsafe_base64_decode(uidb64))
            user = User.objects.get(email=email)
        except(TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None

        if user is not None and \
           default_token_generator.check_token(user, activation_token):

            if user.is_verified is True:
                message = {
                    "message":
                    'Your account is already verified, Please login.'}
                return Response(message, status=status.HTTP_200_OK)
            user.is_verified = True
            user.save()
            self.mail_subject = "Account Verification Confirmation."
            self.message = """
                Hi {},
                Your Author Haven Account has been successfully verified.
                Thank you
                """.format(user.username)
            SendEmail.send_email(self, self.mail_subject, self.message,
                                 user.email)
            message = {
                "message":
                'Email confirmed. Now you can login your account.'}
            return Response(message, status=status.HTTP_200_OK)
        return Response({"error": 'Activation link is invalid or expired !!'},
                        status=status.HTTP_400_BAD_REQUEST)


class SendEmailPasswordReset(generics.CreateAPIView):
    """post: User send email for password reset."""
    permission_classes = (AllowAny,)
    serializer_class = SendPasswordResetEmailSerializer
    renderer_classes = (UserJSONRenderer,)

    def post(self, request):
        email = request.data.get('email')
        callback_url = request.data.get('callback_url')

        if not User.objects.filter(email=email).exists():
            raise serializers.ValidationError({"error":
                                               "User with that email"
                                               " does not exist"},
                                              code=400)

        if callback_url == None:
            raise serializers.ValidationError({"error":
                                               "Please supply the callback url"}, code=400)

        dt = datetime.now()+timedelta(days=1)
        reset_password_token = jwt.encode({'email': email, 'exp': int(
            dt.strftime('%s'))}, settings.SECRET_KEY, 'HS256').decode('utf-8')
        self.mail_subject = "Reset password for your Authors Haven account."
        self.message = """
            Hi,
            Please click on the link to reset your password,
            {}?{}""".format(callback_url,
                            reset_password_token)
        SendEmail.send_email(self, self.mail_subject, self.message, email)
        return Response({"message":
                         "We have sent you an email to reset your password",
                         'reset_password_token': reset_password_token},
                        status=status.HTTP_200_OK)


class ResetPassword(generics.GenericAPIView):
    """put: Email password reset."""
    permission_classes = (AllowAny,)
    look_url_kwarg = 'reset_password_token'
    serializer_class = UpdatePasswordSerializer
    renderer_classes = (UserJSONRenderer,)

    def put(self, request, *args, **kwargs):
        token = self.kwargs.get(self.look_url_kwarg)
        new_password = request.data.get('new_password')
        confirm_password = request.data.get('confirm_password')

        if (new_password != confirm_password):
            # to override Django's built-in validation errors
            raise serializers.ValidationError(
                {"error":
                 "The passwords do not match"})
        elif (
            re.compile(
                r'^(?=.*[A-Za-z])(?=.*\d)(?=.*[$@$!%*#?&])[A-Za-z\d$@$!%*#?&]{8,}$'
            ).search(new_password)
                is None):
            raise serializers.ValidationError(
                {"error":
                 "Ensure your password is alphanumeric, with Minimum eight "
                 "characters, at least one letter, one number and one"
                 " special character"}
            )
        decode_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=['HS256'])
        user = User.objects.get(email=decode_token['email'])
        user.set_password(new_password)
        user.save()
        return Response({'message':
                         'you have successfully changed your password'},
                        status=status.HTTP_200_OK)


class OauthAPIView(generics.GenericAPIView):
    """get: User signup using social authentication."""
    permission_classes = (AllowAny,)

    def get(self, request, social_auth_Provider, auth_provider_url=None):
        social_auth_Provider = social_auth_Provider.lower()
        if social_auth_Provider == 'google':
            auth_provider_url = reverse(
                'social:begin', args=('google-oauth2',))
        if social_auth_Provider == 'facebook':
            auth_provider_url = reverse('social:begin', args=('facebook',))
        if social_auth_Provider == 'twitter':
            auth_provider_url = reverse('social:begin', args=('twitter',))
        if auth_provider_url is not None:
            redirect_to_login_url = """{}://{}{}""".format(
                request.scheme, request.get_host(), auth_provider_url)
            message = message = {
                "status_code": 200,
                "results": {
                    "message": redirect_to_login_url
                }
            }
            return Response(message, status.HTTP_200_OK)
        message = message = {
            "status_code": 400,
            "results": {
                "error": "Please Login with google,"
                "facebook or twitter"
            }
        }
        return Response(message, status.HTTP_400_BAD_REQUEST)


class OauthlLoginAPIView(APIView):  # pragma: no cover
    """get: User login using social authentication."""
    permission_classes = (AllowAny,)

    def get(self, request):  # pragma: no cover
        try:
            response = {
                "username": auth_user.username,
                "email": auth_user.email,
                "token": auth_user.token
            }
            return Response(response, status.HTTP_200_OK)  # pragma: no cover
        except(NameError):  # pragma: no cover
            message = {
                "status_code": 400,
                "results": {
                    "error": "Unable to login, please try again"
                }
            }
            return Response(message,
                            status.HTTP_400_BAD_REQUEST)


class UnsubscribeNotifications(generics.GenericAPIView):

    serializer_class = UnsubscribeNotificationsSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    lookup_field = 'pk'
    renderer_classes = (UserJSONRenderer,)

    def put(self, request, *args, **kwargs):
        user_id = kwargs['pk']
        user, username_current = get_id_from_token(request)
        if user != user_id:
            raise AuthenticationFailed(
                detail="You do not have permissions change this.")

        current_subscirbe_data = User.objects.get(
            pk=user_id)
        subscribe_update = request.data.get(
            'is_subscribed', current_subscirbe_data.is_subscribed)
        new_subscription = {
            "is_subscribed": subscribe_update,
        }
        serializer = UnsubscribeNotificationsSerializer(
            data=new_subscription)
        serializer.is_valid(raise_exception=True)
        serializer.update(current_subscirbe_data, new_subscription)
        return Response(serializer.data, status=status.HTTP_200_OK)


class FacebookAPIView(generics.CreateAPIView):
    """
    Allows social sign using Facebook
    """
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)

    def create(self, request, access_token):

        try:
            # we obtain details of the user from the access token
            graph = facebook.GraphAPI(access_token=access_token)
            user_info = graph.get_object(
                id='me',
                fields='first_name, middle_name,last_name, id, email')
            email = user_info.get('email')
            username = user_info.get('first_name') + \
                ' ' + user_info.get('last_name')
            print(username)
        except facebook.GraphAPIError as e:
            return Response({"error": e.message},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except(TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None
        return RegisterReturnUser(user, username, email)


class GoogleAPIView(generics.CreateAPIView):
    """
    Allows social sign using Google
    """
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)

    def create(self, request, access_token):

        try:
            results = urllib.request.urlopen(
                f"https://www.googleapis.com/oauth2/v1/userinfo?access_token={access_token}").read()
            user_details = json.loads(results.decode())
            email = user_details.get('email')
            username = user_details.get('name')
        except:
            return Response({"error": "The Token is Invalid or expired"},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except(TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None
        return RegisterReturnUser(user, username, email)


class TwitterAPIView(generics.CreateAPIView):
    """
    Allows social sign using Google
    """
    permission_classes = (AllowAny,)
    # renderer_classes = (UserJSONRenderer,)

    def create(self, request, access_key, access_secret,
               http_method="GET", post_body=b"", http_headers=None):

        try:
            url = "https://api.twitter.com/1.1/account/verify_credentials.json?include_email=true"
            consumer = oauth2.Consumer(key=config(
                'SOCIAL_AUTH_TWITTER_KEY'),
                secret=config('SOCIAL_AUTH_TWITTER_SECRET'))
            token = oauth2.Token(key=access_key, secret=access_secret)
            client = oauth2.Client(consumer, token)
            resp, content = client.request(
                url, method=http_method, body=post_body, headers=http_headers)

            user_details = json.loads(content.decode())
            email = user_details.get('email')
            username = user_details.get('screen_name')
            if email is None or username is None:
                return Response({"error": "The Token is Invalid or expired"},
                                status=status.HTTP_400_BAD_REQUEST)

        except KeyError:
            return Response({"error": "The Token is Invalid or expired"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(email=email)
        except(TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None
        return RegisterReturnUser(user, username, email)


def RegisterReturnUser(user, username, email):
    if user:
        return Response({
            'email': user.email,
            'username': user.username,
            'token': user.token
        }, status=status.HTTP_200_OK)
    else:
        user = User(username=username, email=email)
        password = User.objects.make_random_password()
        user.set_password(password)
        user.save()
        user.profile = Profile()
        user.profile.save()
        user.is_verified = True
        return Response({
            'email': user.email,
            'username': user.username,
            'token': user.token
        }, status=status.HTTP_200_OK)
