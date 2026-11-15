"""Lambda authorizer (REQUEST type): verifies the administrator JWT and its role (FR12)."""
import os

from common import jwt_utils, logs


def _policy(principal, effect, resource, context):
    return {
        "principalId": principal,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": resource}],
        },
        "context": context,
    }


def _stage_wildcard(method_arn):
    # arn:aws:execute-api:region:account:apiId/stage/METHOD/path -> .../stage/*/*  (the result is cached per token)
    base, _, rest = method_arn.partition("/")
    return f"{base}/{rest.split('/')[0]}/*/*"


def handler(event, context):
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    value = headers.get("authorization", "")
    token = value[7:] if value.lower().startswith("bearer ") else value
    try:
        claims = jwt_utils.verify(token, os.environ["JWT_SECRET"])
    except jwt_utils.TokenError as exc:
        logs.log("WARNING", "auth_rejected", reason=str(exc))
        raise Exception("Unauthorized")  # API Gateway turns this exact message into a 401

    role = str(claims.get("role"))
    effect = "Allow" if role == "ADMIN" else "Deny"  # Deny -> 403 for any other role
    if effect == "Deny":
        logs.log("WARNING", "auth_forbidden", user=claims.get("sub"), role=role)
    return _policy(str(claims.get("sub", "unknown")), effect, _stage_wildcard(event["methodArn"]),
                   {"role": role, "user_id": str(claims.get("sub", ""))})
