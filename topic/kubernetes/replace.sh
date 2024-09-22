#!/bin/bash
export ACCOUNT_ID=$(aws sts get-caller-identity --output text --query Account)
export AWS_REGION=us-west-1
export AZS=($(aws ec2 describe-availability-zones --query 'AvailabilityZones[].ZoneName' --output text --region $AWS_REGION))
export Domain_Name='xxx.com'
export C_Name='xxx'

alias grep='grep --color=auto'


echo "Query ACCOUNT_ID"
grep -r "${ACCOUNT_ID}" *
#'s@'"${ACCOUNT_ID}"'@<ACCOUNT_ID>@g'

echo "Query Domain Name"
grep -r "${Domain_Name}" *
#'s@'"${Domain_Name}"'@cici.com@g'

grep -r "\.com[[:space:][:punct:]]" *
grep -r "\.amazonaws.com[[:space:][:punct:]]" *
grep -r "\.net[[:space:][:punct:]]" *
grep -r "\.cn[[:space:][:punct:]]" *
grep -r "\.online[[:space:][:punct:]]" *

echo "Query Cn Name"
grep -ri "${C_Name}" *
#'s@'"${C_Name}"'@ciciname@g'

echo "Query AWS AK SK"
grep -ri 'AWS_ACCESS_KEY_ID' *
grep -ri 'AWS_SECRET_ACCESS_KEY' *


echo "Query Aliyun AK"
grep -ri 'Ali_Key' *
grep -ri 'Ali_Secret' *

echo "Query Password"
grep -ri 'passwd' *
grep -ri 'password' *