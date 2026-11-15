"""Creates (or replaces) a user in the SensorWatchUsers table.

Run from the backend folder, with the Learner Lab credentials configured (aws configure):
  python scripts/create_admin.py --user admin --password "a-strong-password"
  python scripts/create_admin.py --user tech1 --password "..." --role TECHNICIAN --email tech1@example.com
"""
import argparse
import pathlib
import sys

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common import jwt_utils  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Create a SensorWatch user")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="ADMIN", choices=["ADMIN", "TECHNICIAN"])
    parser.add_argument("--name")
    parser.add_argument("--email")
    parser.add_argument("--table", default="SensorWatchUsers")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    if len(args.password) < 8:
        sys.exit("The password must have at least 8 characters")

    item = {"user_id": args.user, "role": args.role, "password_hash": jwt_utils.hash_password(args.password)}
    if args.name:
        item["name"] = args.name
    if args.email:
        item["email"] = args.email
    boto3.resource("dynamodb", region_name=args.region).Table(args.table).put_item(Item=item)
    print(f"User '{args.user}' ({args.role}) saved in {args.table}")


if __name__ == "__main__":
    main()
