# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE.txt in the project root for
# license information.
# -------------------------------------------------------------------------
import time
from typing import TYPE_CHECKING, Optional, TypeVar, MutableMapping
from azure.core.pipeline import PipelineRequest, PipelineResponse
from azure.core.pipeline.transport import HttpResponse as LegacyHttpResponse, HttpRequest as LegacyHttpRequest
from azure.core.rest import HttpResponse, HttpRequest
from . import HTTPPolicy, SansIOHTTPPolicy
from ...exceptions import ServiceRequestError

if TYPE_CHECKING:
    # pylint:disable=unused-import
    from azure.core.credentials import (
        AccessToken,
        TokenCredential,
        AzureKeyCredential,
        AzureSasCredential,
    )

HTTPResponseType = TypeVar("HTTPResponseType", HttpResponse, LegacyHttpResponse)
HTTPRequestType = TypeVar("HTTPRequestType", HttpRequest, LegacyHttpRequest)


# pylint:disable=too-few-public-methods
class _BearerTokenCredentialPolicyBase:
    """Base class for a Bearer Token Credential Policy.

    :param credential: The credential.
    :type credential: ~azure.core.credentials.TokenCredential
    :param str scopes: Lets you specify the type of access needed.
    """

    def __init__(self, credential: "TokenCredential", *scopes: str, **kwargs) -> None:  # pylint:disable=unused-argument
        super(_BearerTokenCredentialPolicyBase, self).__init__()
        self._scopes = scopes
        self._credential = credential
        self._token: Optional["AccessToken"] = None

    @staticmethod
    def _enforce_https(request: PipelineRequest[HTTPRequestType]) -> None:
        # move 'enforce_https' from options to context so it persists
        # across retries but isn't passed to a transport implementation
        option = request.context.options.pop("enforce_https", None)

        # True is the default setting; we needn't preserve an explicit opt in to the default behavior
        if option is False:
            request.context["enforce_https"] = option

        enforce_https = request.context.get("enforce_https", True)
        if enforce_https and not request.http_request.url.lower().startswith("https"):
            raise ServiceRequestError(
                "Bearer token authentication is not permitted for non-TLS protected (non-https) URLs."
            )

    @staticmethod
    def _update_headers(headers: MutableMapping[str, str], token: str) -> None:
        """Updates the Authorization header with the bearer token.

        :param MutableMapping[str, str] headers: The HTTP Request headers
        :param str token: The OAuth token.
        """
        headers["Authorization"] = "Bearer {}".format(token)

    @property
    def _need_new_token(self) -> bool:
        return not self._token or self._token.expires_on - time.time() < 300


class BearerTokenCredentialPolicy(_BearerTokenCredentialPolicyBase, HTTPPolicy[HTTPRequestType, HTTPResponseType]):
    """Adds a bearer token Authorization header to requests.

    :param credential: The credential.
    :type credential: ~azure.core.TokenCredential
    :param str scopes: Lets you specify the type of access needed.
    :raises: :class:`~azure.core.exceptions.ServiceRequestError`
    """

    def on_request(self, request: PipelineRequest[HTTPRequestType]) -> None:
        """Called before the policy sends a request.

        The base implementation authorizes the request with a bearer token.

        :param ~azure.core.pipeline.PipelineRequest request: the request
        """
        self._enforce_https(request)

        if self._token is None or self._need_new_token:
            self._token = self._credential.get_token(*self._scopes)
        self._update_headers(request.http_request.headers, self._token.token)

    def authorize_request(self, request: PipelineRequest[HTTPRequestType], *scopes: str, **kwargs) -> None:
        """Acquire a token from the credential and authorize the request with it.

        Keyword arguments are passed to the credential's get_token method. The token will be cached and used to
        authorize future requests.

        :param ~azure.core.pipeline.PipelineRequest request: the request
        :param str scopes: required scopes of authentication
        """
        self._token = self._credential.get_token(*scopes, **kwargs)
        self._update_headers(request.http_request.headers, self._token.token)

    def send(self, request: PipelineRequest[HTTPRequestType]) -> PipelineResponse[HTTPRequestType, HTTPResponseType]:
        """Authorize request with a bearer token and send it to the next policy

        :param request: The pipeline request object
        :type request: ~azure.core.pipeline.PipelineRequest
        :return: The pipeline response object
        :rtype: ~azure.core.pipeline.PipelineResponse
        """
        self.on_request(request)
        try:
            response = self.next.send(request)
            self.on_response(request, response)
        except Exception:  # pylint:disable=broad-except
            self.on_exception(request)
            raise
        else:
            if response.http_response.status_code == 401:
                self._token = None  # any cached token is invalid
                if "WWW-Authenticate" in response.http_response.headers:
                    request_authorized = self.on_challenge(request, response)
                    if request_authorized:
                        # if we receive a challenge response, we retrieve a new token
                        # which matches the new target. In this case, we don't want to remove
                        # token from the request so clear the 'insecure_domain_change' tag
                        request.context.options.pop("insecure_domain_change", False)
                        try:
                            response = self.next.send(request)
                            self.on_response(request, response)
                        except Exception:  # pylint:disable=broad-except
                            self.on_exception(request)
                            raise

        return response

    def on_challenge(
        self, request: PipelineRequest[HTTPRequestType], response: PipelineResponse[HTTPRequestType, HTTPResponseType]
    ) -> bool:
        """Authorize request according to an authentication challenge

        This method is called when the resource provider responds 401 with a WWW-Authenticate header.

        :param ~azure.core.pipeline.PipelineRequest request: the request which elicited an authentication challenge
        :param ~azure.core.pipeline.PipelineResponse response: the resource provider's response
        :returns: a bool indicating whether the policy should send the request
        :rtype: bool
        """
        # pylint:disable=unused-argument
        return False

    def on_response(
        self, request: PipelineRequest[HTTPRequestType], response: PipelineResponse[HTTPRequestType, HTTPResponseType]
    ) -> None:
        """Executed after the request comes back from the next policy.

        :param request: Request to be modified after returning from the policy.
        :type request: ~azure.core.pipeline.PipelineRequest
        :param response: Pipeline response object
        :type response: ~azure.core.pipeline.PipelineResponse
        """

    def on_exception(self, request: PipelineRequest[HTTPRequestType]) -> None:
        """Executed when an exception is raised while executing the next policy.

        This method is executed inside the exception handler.

        :param request: The Pipeline request object
        :type request: ~azure.core.pipeline.PipelineRequest
        """
        # pylint: disable=unused-argument
        return


class AzureKeyCredentialPolicy(SansIOHTTPPolicy[HTTPRequestType, HTTPResponseType]):
    """Adds a key header for the provided credential.

    :param credential: The credential used to authenticate requests.
    :type credential: ~azure.core.credentials.AzureKeyCredential
    :param str name: The name of the key header used for the credential.
    :keyword str prefix: The name of the prefix for the header value if any.
    :raises: ValueError or TypeError
    """

    def __init__(
        self,
        credential: "AzureKeyCredential",
        name: str,
        *,
        prefix: Optional[str] = None,
        **kwargs,  # pylint: disable=unused-argument
    ) -> None:
        super().__init__()
        if not hasattr(credential, "key"):
            raise TypeError("String is not a supported credential input type. Use an instance of AzureKeyCredential.")
        if not name:
            raise ValueError("name can not be None or empty")
        if not isinstance(name, str):
            raise TypeError("name must be a string.")
        self._credential = credential
        self._name = name
        self._prefix = prefix + " " if prefix else ""

    def on_request(self, request: PipelineRequest[HTTPRequestType]) -> None:
        request.http_request.headers[self._name] = f"{self._prefix}{self._credential.key}"


class AzureSasCredentialPolicy(SansIOHTTPPolicy[HTTPRequestType, HTTPResponseType]):
    """Adds a shared access signature to query for the provided credential.

    :param credential: The credential used to authenticate requests.
    :type credential: ~azure.core.credentials.AzureSasCredential
    :raises: ValueError or TypeError
    """

    def __init__(self, credential: "AzureSasCredential", **kwargs) -> None:  # pylint: disable=unused-argument
        super(AzureSasCredentialPolicy, self).__init__()
        if not credential:
            raise ValueError("credential can not be None")
        self._credential = credential

    def on_request(self, request: PipelineRequest[HTTPRequestType]) -> None:
        url = request.http_request.url
        query = request.http_request.query
        signature = self._credential.signature
        if signature.startswith("?"):
            signature = signature[1:]
        if query:
            if signature not in url:
                url = url + "&" + signature
        else:
            if url.endswith("?"):
                url = url + signature
            else:
                url = url + "?" + signature
        request.http_request.url = url
