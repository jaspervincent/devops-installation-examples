#db user
#CREATE DATABASE `yearning` CHARACTER SET 'utf8mb4' COLLATE 'utf8mb4_general_ci';
#CREATE USER `yearning`@`%` IDENTIFIED BY 'yearning@!123';
#GRANT ALL PRIVILEGES ON yearning.* TO 'yearning'@'%';

#使用文件挂载不需要执行以下SQL
#use yearning;
#INSERT INTO `core_accounts` (`username`,`password`,`department`,`real_name`,`email`,`is_recorder`) VALUES ('admin','pbkdf2_sha256$120000$LwSop65y17f4$AgXGkzmZLF7Lw3K1gToLkOBHqziBC0Wpf3bBIHoWA5Q=','DBA','超级管理员','',0);

#INSERT INTO `core_global_configurations` (`authorization`,`ldap`,`message`,`other`,`stmt`,`audit_role`,`board`) VALUES ('global','{"url":"","user":"","password":"","type":"(\u0026(objectClass=organizationalPerson)   (sAMAccountName=%s))","sc":"","ldaps":false,"map":"","test_user":"","test_password":""}','{"web_hook":"","host":"","port":25,"user":"","password":"","to_user":"","mail":false,"ding":false,"ssl":false,"push_type":false,"key":""}','{"limit":1000,"idc":["Aliyun","AWS"],"query":false,"register":false,"export":false,"ex_query_time":60}',0,'{"DMLAllowLimitSTMT":false,"DMLInsertColumns":false,"DMLMaxInsertRows":10,"DMLWhere":false,"DMLAllowInsertNull":false,"DMLOrder":false,"DMLSelect":false,"DMLInsertMustExplicitly":false,"DDLEnablePrimaryKey":false,"DDLCheckTableComment":false,"DDlCheckColumnComment":false,"DDLCheckColumnNullable":false,"DDLCheckColumnDefault":false,"DDLEnableAcrossDBRename":false,"DDLEnableAutoincrementInit":false,"DDLEnableAutoIncrement":false,"DDLEnableAutoincrementUnsigned":false,"DDLEnableDropTable":false,"DDLEnableDropDatabase":false,"DDLEnableNullIndexName":false,"DDLIndexNameSpec":false,"DDLMaxKeyParts":5,"DDLMaxKey":5,"DDLMaxCharLength":10,"MaxTableNameLen":10,"MaxAffectRows":1000,"MaxDDLAffectRows":0,"SupportCharset":"","SupportCollation":"","CheckIdentifier":false,"MustHaveColumns":"","DDLMultiToCommit":false,"DDLPrimaryKeyMust":false,"DDLAllowColumnType":false,"DDLImplicitTypeConversion":false,"DDLAllowPRINotInt":false,"DDLEnableForeignKey":false,"DDLTablePrefix":"","DDLColumnsMustHaveIndex":"","DDLAllowChangeColumnPosition":false,"DDLCheckFloatDouble":false,"IsOSC":false,"OSCExpr":"","OscSize":0,"AllowCreateView":false,"AllowCrateViewWithSelectStar":false,"AllowCreatePartition":false,"AllowSpecialType":false,"PRIRollBack":false}','');

#INSERT INTO `core_graineds` (`username`,`group`) VALUES ('admin','["admin"]');
#admin密码Yearning_admin

conf='
addr=testt-2.rds.amazonaws.com
user=yearning
pass=yearning@!123
data=yearning
sk=JHcrCBRuCWey2dI6
'


#kubectl create cm conf-tmp --from-env-file=<(echo "$conf") --dry-run=client -oyaml
#kubectl -n infra create secret  generic yearning-conf --from-env-file=<(echo "$conf") --dry-run=client -oyaml
kubectl -n infra create secret  generic yearning-conf --from-env-file=<(echo "$conf")
kubectl -n infra get secret yearning-conf -oyaml
