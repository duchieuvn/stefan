## Export data dump to original Postgres db

sudo pg_dump -U holzofen -h localhost -F c -b -v -f "/home/holzofen/Schreibtisch/data_backup/sensor_data.dump" sensor_data
password: biofilter

## Download data dump to destination machine

scp holzofen@10.10.64.27:/home/holzofen/Schreibtisch/data_backup/sensor_data.dump /home/duchieuvn/Code/stefan/postgres_schedule/

## Build and run docker container

docker compose up -d
