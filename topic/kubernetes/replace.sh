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
grep -rE '\bAKIA[A-Za-z0-9]{12,40}\b' *

echo "Query Aliyun AK"
grep -ri 'Ali_Key' *
grep -ri 'Ali_Secret' *
grep -rE '\bLTAI[A-Za-z0-9]{12,30}\b' *

grep -rE '\bAKID[A-Za-z0-9]{13,40}\b' * #腾讯云
grep -rE '\bJDC_[0-9A-Z]{25,40}\b' * #京东云
grep -rE '\b(?:AKLT|AKTP)[a-zA-Z0-9]{35,50}\b' * #火山引擎
grep -rE '\bAKLT[a-zA-Z0-9\_\-]{16,28}\b' *  #金山云

echo "Query Password"
grep -ri 'passwd' *
grep -ri 'password' *