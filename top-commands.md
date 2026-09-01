- list running python processes  
pgrep -fa python

- background python (-u is just writing log without buffer)
nohup python -u your_script.py > output.log 2>&1 &

- no log output  
nohup python -u your_script.py > /dev/null 2>&1 &