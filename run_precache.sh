#!/bin/bash

# Naviga nella directory del progetto
cd "$(dirname "$0")"

# Attiva il virtualenv se esiste
if [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "Avvio precache di tutti i campionati in background..."
echo "I log verranno salvati in precache.log"

# Esegue lo script in background con nohup
nohup python3 precache_all.py > precache.log 2>&1 &

echo "Script avviato con PID: $!"
echo "Puoi monitorare l'avanzamento con: tail -f precache.log"
