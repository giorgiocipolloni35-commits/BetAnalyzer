"""
FIFA World Cup 2026 — Squad management & analysis.

Imports official squad lists, matches players to our existing DB
(player_info + player_stats_cache) for ratings, goals, cards, minutes.
"""

import sqlite3
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "betanalyzer.db"

# ══════════════════════════════════════════════════════════════
# OFFICIAL SQUADS — FIFA World Cup 2026
# Format: {country: {group, coach, players: [{name, pos, club}]}}
# ══════════════════════════════════════════════════════════════

WC_SQUADS = {
    "Corea del Sud": {
        "group": "A",
        "coach": "Hong Myung-bo",
        "players": [
            {"name": "Kim Seung-gyu", "pos": "GK", "club": "FC Tokyo"},
            {"name": "Song Bum-keun", "pos": "GK", "club": "Jeonbuk Hyundai"},
            {"name": "Jo Hyeon-woo", "pos": "GK", "club": "Ulsan HD"},
            {"name": "Kim Moon-hwan", "pos": "DEF", "club": "Daejeon Hana Citizen"},
            {"name": "Kim Min-jae", "pos": "DEF", "club": "Bayern Monaco"},
            {"name": "Kim Tae-hyeon", "pos": "DEF", "club": "Kashima Antlers"},
            {"name": "Park Jin-seob", "pos": "DEF", "club": "Zhejiang Zhiye"},
            {"name": "Seol Young-woo", "pos": "DEF", "club": "Stella Rossa"},
            {"name": "Jens Castrop", "pos": "DEF", "club": "Borussia Monchengladbach"},
            {"name": "Lee Ki-hyuk", "pos": "DEF", "club": "Gangwon"},
            {"name": "Lee Tae-seok", "pos": "DEF", "club": "Austria Vienna"},
            {"name": "Lee Han-beom", "pos": "DEF", "club": "Midtjylland"},
            {"name": "Cho Yu-min", "pos": "DEF", "club": "Sharjah"},
            {"name": "Kim Jin-gyu", "pos": "MID", "club": "Jeonbuk Hyundai"},
            {"name": "Bae Jun-ho", "pos": "MID", "club": "Stoke City"},
            {"name": "Paik Seung-ho", "pos": "MID", "club": "Birmingham City"},
            {"name": "Yang Hyun-jun", "pos": "MID", "club": "Celtic"},
            {"name": "Eom Ji-sung", "pos": "MID", "club": "Swansea City"},
            {"name": "Lee Kang-in", "pos": "MID", "club": "PSG"},
            {"name": "Lee Dong-gyeong", "pos": "MID", "club": "Ulsan HD"},
            {"name": "Lee Jae-sung", "pos": "MID", "club": "Mainz"},
            {"name": "Hwang In-beom", "pos": "MID", "club": "Feyenoord"},
            {"name": "Hwang Hee-chan", "pos": "MID", "club": "Wolverhampton"},
            {"name": "Son Heung-min", "pos": "ATT", "club": "Los Angeles FC"},
            {"name": "Oh Hyeon-gyu", "pos": "ATT", "club": "Besiktas"},
            {"name": "Cho Gue-sung", "pos": "ATT", "club": "Midtjylland"},
        ],
    },
    "Bosnia": {
        "group": "B",
        "coach": "Sergej Barbarez",
        "players": [
            {"name": "Vasilj", "pos": "GK", "club": "St Pauli"},
            {"name": "Zlomislic", "pos": "GK", "club": "Rijeka"},
            {"name": "Hadzikic", "pos": "GK", "club": "Slaven Belupo"},
            {"name": "Dedic", "pos": "DEF", "club": "Benfica"},
            {"name": "Katic", "pos": "DEF", "club": "Schalke 04"},
            {"name": "Radeljic", "pos": "DEF", "club": "Rijeka"},
            {"name": "Celik", "pos": "DEF", "club": "Lens"},
            {"name": "Mujakic", "pos": "DEF", "club": "Eyupspor"},
            {"name": "Muharemovic", "pos": "DEF", "club": "Sassuolo"},
            {"name": "Kolasinac", "pos": "DEF", "club": "Atalanta"},
            {"name": "Hadzikadunic", "pos": "DEF", "club": "Sampdoria"},
            {"name": "Hadziahmetovic", "pos": "MID", "club": "Hull City"},
            {"name": "Sunjic", "pos": "MID", "club": "Pafos"},
            {"name": "Tahirovic", "pos": "MID", "club": "Brondby"},
            {"name": "Burnic", "pos": "MID", "club": "Karlsruhe"},
            {"name": "Gigovic", "pos": "MID", "club": "Young Boys"},
            {"name": "Mahmic", "pos": "MID", "club": "Slovan Liberec"},
            {"name": "Basic", "pos": "MID", "club": "Astana"},
            {"name": "Memic", "pos": "MID", "club": "Viktoria Plzen"},
            {"name": "Alajbegovic", "pos": "MID", "club": "Salisburgo"},
            {"name": "Bajraktarevic", "pos": "MID", "club": "PSV"},
            {"name": "Bazdar", "pos": "ATT", "club": "Jagiellonia"},
            {"name": "Lukic", "pos": "ATT", "club": "Cluj"},
            {"name": "Tabakovic", "pos": "ATT", "club": "Borussia Monchengladbach"},
            {"name": "Demirovic", "pos": "ATT", "club": "Stoccarda"},
            {"name": "Dzeko", "pos": "ATT", "club": "Schalke 04"},
        ],
    },
    "Svizzera": {
        "group": "B",
        "coach": "Murat Yakin",
        "players": [
            {"name": "Kobel", "pos": "GK", "club": "Borussia Dortmund"},
            {"name": "Keller", "pos": "GK", "club": "Young Boys"},
            {"name": "Mvogo", "pos": "GK", "club": "Lorient"},
            {"name": "Ricardo Rodriguez", "pos": "DEF", "club": "Betis Siviglia"},
            {"name": "Akanji", "pos": "DEF", "club": "Inter"},
            {"name": "Widmer", "pos": "DEF", "club": "Mainz"},
            {"name": "Elvedi", "pos": "DEF", "club": "Borussia Monchengladbach"},
            {"name": "Muheim", "pos": "DEF", "club": "Amburgo"},
            {"name": "Amenda", "pos": "DEF", "club": "Eintracht Francoforte"},
            {"name": "Comert", "pos": "DEF", "club": "Valencia"},
            {"name": "Jaquez", "pos": "DEF", "club": "Stoccarda"},
            {"name": "Aebischer", "pos": "MID", "club": "Pisa"},
            {"name": "Fassnacht", "pos": "MID", "club": "Young Boys"},
            {"name": "Freuler", "pos": "MID", "club": "Bologna"},
            {"name": "Jashari", "pos": "MID", "club": "Milan"},
            {"name": "Manzambi", "pos": "MID", "club": "Friburgo"},
            {"name": "Rieder", "pos": "MID", "club": "Augusta"},
            {"name": "Sow", "pos": "MID", "club": "Siviglia"},
            {"name": "Xhaka", "pos": "MID", "club": "Sunderland"},
            {"name": "Zakaria", "pos": "MID", "club": "Monaco"},
            {"name": "Amdouni", "pos": "ATT", "club": "Burnley"},
            {"name": "Embolo", "pos": "ATT", "club": "Rennes"},
            {"name": "Itten", "pos": "ATT", "club": "Fortuna Dusseldorf"},
            {"name": "Ndoye", "pos": "ATT", "club": "Nottingham Forest"},
            {"name": "Okafor", "pos": "ATT", "club": "Leeds"},
            {"name": "Vargas", "pos": "ATT", "club": "Siviglia"},
        ],
    },
    "Brasile": {
        "group": "C",
        "coach": "Carlo Ancelotti",
        "players": [
            {"name": "Alisson", "pos": "GK", "club": "Liverpool"},
            {"name": "Ederson", "pos": "GK", "club": "Fenerbahce"},
            {"name": "Weverton", "pos": "GK", "club": "Gremio"},
            {"name": "Alex Sandro", "pos": "DEF", "club": "Flamengo"},
            {"name": "Bremer", "pos": "DEF", "club": "Juventus"},
            {"name": "Danilo", "pos": "DEF", "club": "Flamengo"},
            {"name": "Douglas Santos", "pos": "DEF", "club": "Zenit"},
            {"name": "Gabriel", "pos": "DEF", "club": "Arsenal"},
            {"name": "Ibanez", "pos": "DEF", "club": "Al-Ahli"},
            {"name": "Leo Pereira", "pos": "DEF", "club": "Flamengo"},
            {"name": "Marquinhos", "pos": "DEF", "club": "PSG"},
            {"name": "Wesley", "pos": "DEF", "club": "Roma"},
            {"name": "Bruno Guimaraes", "pos": "MID", "club": "Newcastle"},
            {"name": "Casemiro", "pos": "MID", "club": "Manchester United"},
            {"name": "Danilo", "pos": "MID", "club": "Botafogo"},
            {"name": "Lucas Paqueta", "pos": "MID", "club": "Flamengo"},
            {"name": "Fabinho", "pos": "MID", "club": "Al-Ittihad"},
            {"name": "Endrick", "pos": "ATT", "club": "Lione"},
            {"name": "Martinelli", "pos": "ATT", "club": "Arsenal"},
            {"name": "Igor Thiago", "pos": "ATT", "club": "Brentford"},
            {"name": "Luis Henrique", "pos": "ATT", "club": "Zenit"},
            {"name": "Matheus Cunha", "pos": "ATT", "club": "Manchester United"},
            {"name": "Neymar", "pos": "ATT", "club": "Santos"},
            {"name": "Rayan", "pos": "ATT", "club": "Bournemouth"},
            {"name": "Vinicius Jr", "pos": "ATT", "club": "Real Madrid"},
            {"name": "Raphinha", "pos": "ATT", "club": "Barcellona"},
        ],
    },
    "Haiti": {
        "group": "C",
        "coach": "Sebastien Migne",
        "players": [
            {"name": "Placide", "pos": "GK", "club": "Bastia"},
            {"name": "A. Pierre", "pos": "GK", "club": "Sochaux"},
            {"name": "Duverger", "pos": "GK", "club": "Cosmos Koblenz"},
            {"name": "Arcus", "pos": "DEF", "club": "Angers"},
            {"name": "Paugain", "pos": "DEF", "club": "Zulte Waregem"},
            {"name": "Lacroix", "pos": "DEF", "club": "Colorado Springs"},
            {"name": "Experience", "pos": "DEF", "club": "Nancy"},
            {"name": "Duverne", "pos": "DEF", "club": "Gent"},
            {"name": "Ade", "pos": "DEF", "club": "LDU Quito"},
            {"name": "Delcroix", "pos": "DEF", "club": "Lugano"},
            {"name": "Thermoncy", "pos": "DEF", "club": "Young Boys"},
            {"name": "Sainte", "pos": "MID", "club": "El Paso Locomotive"},
            {"name": "L. Pierre", "pos": "MID", "club": "Vizela"},
            {"name": "Danley", "pos": "MID", "club": "Philadelphia Union"},
            {"name": "Bellegarde", "pos": "MID", "club": "Wolves"},
            {"name": "W. Pierre", "pos": "MID", "club": "Violette"},
            {"name": "Simon", "pos": "MID", "club": "FC Tatran Presov"},
            {"name": "Deedson", "pos": "ATT", "club": "FC Dallas"},
            {"name": "Casimir", "pos": "ATT", "club": "Auxerre"},
            {"name": "Etienne", "pos": "ATT", "club": "Toronto FC"},
            {"name": "Providence", "pos": "ATT", "club": "Almere"},
            {"name": "Nazon", "pos": "ATT", "club": "Esteghlal"},
            {"name": "Pierrot", "pos": "ATT", "club": "Caykur Rizespor"},
            {"name": "Isidor", "pos": "ATT", "club": "Sunderland"},
            {"name": "Fortune", "pos": "ATT", "club": "Vizela"},
            {"name": "Joseph", "pos": "ATT", "club": "Ferencvaros"},
        ],
    },
    "Scozia": {
        "group": "C",
        "coach": "Steve Clarke",
        "players": [
            {"name": "Gordon", "pos": "GK", "club": "Hearts"},
            {"name": "Gunn", "pos": "GK", "club": "Nottingham Forest"},
            {"name": "Kelly", "pos": "GK", "club": "Rangers"},
            {"name": "Hanley", "pos": "DEF", "club": "Hibernian"},
            {"name": "Hendry", "pos": "DEF", "club": "Al Etiffaq"},
            {"name": "Hickey", "pos": "DEF", "club": "Brentford"},
            {"name": "Hyam", "pos": "DEF", "club": "Wrexham"},
            {"name": "McKenna", "pos": "DEF", "club": "Dinamo Zagabria"},
            {"name": "Patterson", "pos": "DEF", "club": "Everton"},
            {"name": "Ralston", "pos": "DEF", "club": "Celtic"},
            {"name": "Robertson", "pos": "DEF", "club": "Liverpool"},
            {"name": "Souttar", "pos": "DEF", "club": "Rangers"},
            {"name": "Tierney", "pos": "DEF", "club": "Celtic"},
            {"name": "Christie", "pos": "MID", "club": "Bournemouth"},
            {"name": "Curtis", "pos": "MID", "club": "Kilmarnock"},
            {"name": "Ferguson", "pos": "MID", "club": "Bologna"},
            {"name": "Gannon-Doak", "pos": "MID", "club": "Bournemouth"},
            {"name": "Gilmour", "pos": "MID", "club": "Napoli"},
            {"name": "McGinn", "pos": "MID", "club": "Aston Villa"},
            {"name": "McLean", "pos": "MID", "club": "Norwich"},
            {"name": "McTominay", "pos": "MID", "club": "Napoli"},
            {"name": "Adams", "pos": "ATT", "club": "Torino"},
            {"name": "Dykes", "pos": "ATT", "club": "Charlton Athletic"},
            {"name": "Hirst", "pos": "ATT", "club": "Ipswich"},
            {"name": "Shankland", "pos": "ATT", "club": "Hearts"},
            {"name": "Stewart", "pos": "ATT", "club": "Southampton"},
        ],
    },
    "Curacao": {
        "group": "E",
        "coach": "Dick Advocaat",
        "players": [
            {"name": "Bodak", "pos": "GK", "club": "Telstar"},
            {"name": "Doornbusch", "pos": "GK", "club": "Venlo"},
            {"name": "Room", "pos": "GK", "club": "Miami FC"},
            {"name": "Bazoer", "pos": "DEF", "club": "Konyaspor"},
            {"name": "Brenet", "pos": "DEF", "club": "Kayserispor"},
            {"name": "Van Eijma", "pos": "DEF", "club": "RKC Waalwijk"},
            {"name": "Floranus", "pos": "DEF", "club": "PEC Zwolle"},
            {"name": "Fonville", "pos": "DEF", "club": "NEC Nijmegen"},
            {"name": "Gaari", "pos": "DEF", "club": "Abha"},
            {"name": "Obispo", "pos": "DEF", "club": "PSV Eindhoven"},
            {"name": "Sambo", "pos": "DEF", "club": "Sparta Rotterdam"},
            {"name": "Juninho Bacuna", "pos": "MID", "club": "Volendam"},
            {"name": "Leandro Bacuna", "pos": "MID", "club": "Igdir"},
            {"name": "Comenencia", "pos": "MID", "club": "Zurigo"},
            {"name": "Felida", "pos": "MID", "club": "Den Bosch"},
            {"name": "Martha", "pos": "MID", "club": "Rotherham"},
            {"name": "Noslin", "pos": "MID", "club": "Telstar"},
            {"name": "Roemeratoe", "pos": "MID", "club": "RKC Waalwijk"},
            {"name": "Antonisse", "pos": "ATT", "club": "Kifisia"},
            {"name": "Chong", "pos": "ATT", "club": "Sheffield United"},
            {"name": "Gorre", "pos": "ATT", "club": "Maccabi Haifa"},
            {"name": "Hansen", "pos": "ATT", "club": "Middlesbrough"},
            {"name": "Kastaneer", "pos": "ATT", "club": "Terengganu"},
            {"name": "Kuwas", "pos": "ATT", "club": "Volendam"},
            {"name": "Locadia", "pos": "ATT", "club": "Miami FC"},
            {"name": "Margaritha", "pos": "ATT", "club": "Beveren"},
        ],
    },
    "Costa d'Avorio": {
        "group": "E",
        "coach": "Emerse Fae",
        "players": [
            {"name": "Y. Fofana", "pos": "GK", "club": "Caykur Rizespor"},
            {"name": "Kone", "pos": "GK", "club": "Charleroi"},
            {"name": "Lafont", "pos": "GK", "club": "Panathinaikos"},
            {"name": "Agbadou", "pos": "DEF", "club": "Besiktas"},
            {"name": "Akpa", "pos": "DEF", "club": "Auxerre"},
            {"name": "O. Diomande", "pos": "DEF", "club": "Sporting"},
            {"name": "Doue", "pos": "DEF", "club": "Strasburgo"},
            {"name": "Konan", "pos": "DEF", "club": "Gil Vicente"},
            {"name": "Kossounou", "pos": "DEF", "club": "Atalanta"},
            {"name": "Ndicka", "pos": "DEF", "club": "Roma"},
            {"name": "Singo", "pos": "DEF", "club": "Galatasaray"},
            {"name": "Seko Fofana", "pos": "MID", "club": "Porto"},
            {"name": "Guiagon", "pos": "MID", "club": "Charleroi"},
            {"name": "Kessie", "pos": "MID", "club": "Al-Ahli"},
            {"name": "Inao Oulai", "pos": "MID", "club": "Trabzonspor"},
            {"name": "Sangare", "pos": "MID", "club": "Nottingham Forest"},
            {"name": "Seri", "pos": "MID", "club": "Maribor"},
            {"name": "Adingra", "pos": "ATT", "club": "Monaco"},
            {"name": "Bonny", "pos": "ATT", "club": "Inter"},
            {"name": "A. Diallo", "pos": "ATT", "club": "Manchester United"},
            {"name": "Diakite", "pos": "ATT", "club": "Cercle Bruges"},
            {"name": "Y. Diomande", "pos": "ATT", "club": "Lipsia"},
            {"name": "Guessand", "pos": "ATT", "club": "Crystal Palace"},
            {"name": "Pepe", "pos": "ATT", "club": "Villarreal"},
            {"name": "Traore", "pos": "ATT", "club": "Basilea"},
            {"name": "Wahi", "pos": "ATT", "club": "Nizza"},
        ],
    },
    "Giappone": {
        "group": "F",
        "coach": "Hajime Moriyasu",
        "players": [
            {"name": "Hayakawa", "pos": "GK", "club": "Kashima Antlers"},
            {"name": "Osako", "pos": "GK", "club": "Sanfrecce Hiroshima"},
            {"name": "Suzuki", "pos": "GK", "club": "Parma"},
            {"name": "Nagatomo", "pos": "DEF", "club": "FC Tokyo"},
            {"name": "Taniguchi", "pos": "DEF", "club": "Sint-Truiden"},
            {"name": "Itakura", "pos": "DEF", "club": "Ajax"},
            {"name": "Watanabe", "pos": "DEF", "club": "Feyenoord"},
            {"name": "Tomiyasu", "pos": "DEF", "club": "Ajax"},
            {"name": "Ito", "pos": "DEF", "club": "Bayern Monaco"},
            {"name": "Seko", "pos": "DEF", "club": "Le Havre"},
            {"name": "Sugawara", "pos": "DEF", "club": "Werder Brema"},
            {"name": "Suzuki", "pos": "DEF", "club": "Copenaghen"},
            {"name": "Doan", "pos": "MID", "club": "Eintracht Francoforte"},
            {"name": "Endo", "pos": "MID", "club": "Liverpool"},
            {"name": "Ito", "pos": "MID", "club": "Genk"},
            {"name": "Kamada", "pos": "MID", "club": "Crystal Palace"},
            {"name": "Kubo", "pos": "MID", "club": "Real Sociedad"},
            {"name": "Nakamura", "pos": "MID", "club": "Reims"},
            {"name": "Sano", "pos": "MID", "club": "Mainz"},
            {"name": "Tanaka", "pos": "MID", "club": "Leeds"},
            {"name": "Ogawa", "pos": "ATT", "club": "NEC Nijmegen"},
            {"name": "Maeda", "pos": "ATT", "club": "Celtic"},
            {"name": "Ueda", "pos": "ATT", "club": "Feyenoord"},
            {"name": "Suzuki", "pos": "ATT", "club": "Friburgo"},
            {"name": "Shiogai", "pos": "ATT", "club": "Wolfsburg"},
            {"name": "Goto", "pos": "ATT", "club": "Sint-Truiden"},
        ],
    },
    "Svezia": {
        "group": "F",
        "coach": "Graham Potter",
        "players": [
            {"name": "Johansson", "pos": "GK", "club": "Stoke City"},
            {"name": "Nordfeldt", "pos": "GK", "club": "AIK"},
            {"name": "Widell Zetterstrom", "pos": "GK", "club": "Derby County"},
            {"name": "Ekdal", "pos": "DEF", "club": "Burnley"},
            {"name": "Gudmundsson", "pos": "DEF", "club": "Leeds United"},
            {"name": "Hien", "pos": "DEF", "club": "Atalanta"},
            {"name": "Holm", "pos": "DEF", "club": "Juventus"},
            {"name": "Lagerbielke", "pos": "DEF", "club": "Braga"},
            {"name": "Lindelof", "pos": "DEF", "club": "Aston Villa"},
            {"name": "Smith", "pos": "DEF", "club": "St. Pauli"},
            {"name": "Starfelt", "pos": "DEF", "club": "Celta Vigo"},
            {"name": "Stroud", "pos": "DEF", "club": "Mjallby"},
            {"name": "Svensson", "pos": "DEF", "club": "Borussia Dortmund"},
            {"name": "Ali", "pos": "MID", "club": "Malmo"},
            {"name": "Ayari", "pos": "MID", "club": "Brighton"},
            {"name": "Bergvall", "pos": "MID", "club": "Tottenham"},
            {"name": "Karlstrom", "pos": "MID", "club": "Udinese"},
            {"name": "Sema", "pos": "MID", "club": "Pafos"},
            {"name": "Svanberg", "pos": "MID", "club": "Wolfsburg"},
            {"name": "Zeneli", "pos": "MID", "club": "Union St-Gilloise"},
            {"name": "Bernhardsson", "pos": "ATT", "club": "Holstein Kiel"},
            {"name": "Elanga", "pos": "ATT", "club": "Newcastle United"},
            {"name": "Gyokeres", "pos": "ATT", "club": "Arsenal"},
            {"name": "Isak", "pos": "ATT", "club": "Liverpool"},
            {"name": "Nilsson", "pos": "ATT", "club": "Club Brugge"},
            {"name": "Nygren", "pos": "ATT", "club": "Celtic"},
        ],
    },
    "Tunisia": {
        "group": "F",
        "coach": "Sabri Lamouchi",
        "players": [
            {"name": "Dahmen", "pos": "GK", "club": "CS Sfaxien"},
            {"name": "Ben Hassine", "pos": "GK", "club": "Etoile du Sahel"},
            {"name": "Chamakh", "pos": "GK", "club": "Club Africain"},
            {"name": "Talbi", "pos": "DEF", "club": "Lorient"},
            {"name": "Bronn", "pos": "DEF", "club": "Servette"},
            {"name": "Rekik", "pos": "DEF", "club": "Maribor"},
            {"name": "Valery", "pos": "DEF", "club": "Young Boys"},
            {"name": "Abdi", "pos": "DEF", "club": "Nizza"},
            {"name": "Ben Hamida", "pos": "DEF", "club": "Esperance de Tunis"},
            {"name": "Arous", "pos": "DEF", "club": "Kasimpasa"},
            {"name": "Chikhaoui", "pos": "DEF", "club": "Monastir"},
            {"name": "Neffati", "pos": "DEF", "club": "Norrkoping"},
            {"name": "Skhiri", "pos": "MID", "club": "Eintracht Francoforte"},
            {"name": "Mejbri", "pos": "MID", "club": "Burnley"},
            {"name": "Ben Slimane", "pos": "MID", "club": "Norwich"},
            {"name": "Gharbi", "pos": "MID", "club": "Augsburg"},
            {"name": "Hadj Mahmoud", "pos": "MID", "club": "Lugano"},
            {"name": "Khedira", "pos": "MID", "club": "Union Berlino"},
            {"name": "Ben Ouanes", "pos": "MID", "club": "Kasimpasa"},
            {"name": "Achouri", "pos": "ATT", "club": "Copenaghen"},
            {"name": "Ayari", "pos": "ATT", "club": "PSG"},
            {"name": "Chaouat", "pos": "ATT", "club": "Club Africain"},
            {"name": "Elloumi", "pos": "ATT", "club": "Vancouver"},
            {"name": "Mastouri", "pos": "ATT", "club": "Dynamo Makhachkala"},
            {"name": "Saad", "pos": "ATT", "club": "Hannover"},
            {"name": "Tounekti", "pos": "ATT", "club": "Celtic"},
        ],
    },
    "Belgio": {
        "group": "G",
        "coach": "Rudi Garcia",
        "players": [
            {"name": "Courtois", "pos": "GK", "club": "Real Madrid"},
            {"name": "Lammens", "pos": "GK", "club": "Manchester United"},
            {"name": "Penders", "pos": "GK", "club": "Strasburgo"},
            {"name": "Castagne", "pos": "DEF", "club": "Fulham"},
            {"name": "Debast", "pos": "DEF", "club": "Sporting"},
            {"name": "De Cuyper", "pos": "DEF", "club": "Brighton"},
            {"name": "De Winter", "pos": "DEF", "club": "Milan"},
            {"name": "Mechele", "pos": "DEF", "club": "Bruges"},
            {"name": "Meunier", "pos": "DEF", "club": "Lille"},
            {"name": "Ngoy", "pos": "DEF", "club": "Lille"},
            {"name": "Seys", "pos": "DEF", "club": "Bruges"},
            {"name": "Theate", "pos": "DEF", "club": "Eintracht Francoforte"},
            {"name": "De Bruyne", "pos": "MID", "club": "Napoli"},
            {"name": "Onana", "pos": "MID", "club": "Aston Villa"},
            {"name": "Raskin", "pos": "MID", "club": "Rangers"},
            {"name": "Tielemans", "pos": "MID", "club": "Aston Villa"},
            {"name": "Vanaken", "pos": "MID", "club": "Bruges"},
            {"name": "Witsel", "pos": "MID", "club": "Girona"},
            {"name": "De Ketelaere", "pos": "ATT", "club": "Atalanta"},
            {"name": "Doku", "pos": "ATT", "club": "Manchester City"},
            {"name": "Fernandez Pardo", "pos": "ATT", "club": "Lille"},
            {"name": "Lukaku", "pos": "ATT", "club": "Napoli"},
            {"name": "Lukebakio", "pos": "ATT", "club": "Benfica"},
            {"name": "D. Moreira", "pos": "ATT", "club": "Strasburgo"},
            {"name": "Saelemaekers", "pos": "ATT", "club": "Milan"},
            {"name": "Trossard", "pos": "ATT", "club": "Arsenal"},
        ],
    },
    "Nuova Zelanda": {
        "group": "G",
        "coach": "Darren Bazeley",
        "players": [
            {"name": "Crocombe", "pos": "GK", "club": "Millwall"},
            {"name": "Paulsen", "pos": "GK", "club": "Lechia Gdansk"},
            {"name": "Woud", "pos": "GK", "club": "Auckland FC"},
            {"name": "Bindon", "pos": "DEF", "club": "Nottingham Forest"},
            {"name": "Boxall", "pos": "DEF", "club": "Minnesota United"},
            {"name": "Cacace", "pos": "DEF", "club": "Wrexham"},
            {"name": "De Vries", "pos": "DEF", "club": "Auckland FC"},
            {"name": "Elliot", "pos": "DEF", "club": "Auckland FC"},
            {"name": "Payne", "pos": "DEF", "club": "Wellington Phoenix"},
            {"name": "Pijnaker", "pos": "DEF", "club": "Auckland FC"},
            {"name": "Smith", "pos": "DEF", "club": "Braintree Town"},
            {"name": "Surman", "pos": "DEF", "club": "Portland Timbers"},
            {"name": "Bayliss", "pos": "MID", "club": "Newcastle Jets"},
            {"name": "Bell", "pos": "MID", "club": "Viking"},
            {"name": "Rufer", "pos": "MID", "club": "Wellington Phoenix"},
            {"name": "Stamenic", "pos": "MID", "club": "Swansea"},
            {"name": "Thomas", "pos": "MID", "club": "PEC Zwolle"},
            {"name": "Barbarouses", "pos": "ATT", "club": "Western Sydney"},
            {"name": "Garbett", "pos": "ATT", "club": "Peterborough United"},
            {"name": "Just", "pos": "ATT", "club": "Motherwell"},
            {"name": "McCowatt", "pos": "ATT", "club": "Silkeborg"},
            {"name": "Old", "pos": "ATT", "club": "St Etienne"},
            {"name": "Randall", "pos": "ATT", "club": "Auckland FC"},
            {"name": "Singh", "pos": "ATT", "club": "Wellington Phoenix"},
            {"name": "Waine", "pos": "ATT", "club": "Port Vale"},
            {"name": "Wood", "pos": "ATT", "club": "Nottingham Forest"},
        ],
    },
    "Capo Verde": {
        "group": "H",
        "coach": "Bubista",
        "players": [
            {"name": "Dos Santos", "pos": "GK", "club": "San Diego"},
            {"name": "Rosa", "pos": "GK", "club": "Montana"},
            {"name": "Vozinha", "pos": "GK", "club": "Chaves"},
            {"name": "Lopes Cabral", "pos": "DEF", "club": "Benfica"},
            {"name": "Diney Borges", "pos": "DEF", "club": "Al-Bataeh"},
            {"name": "Logan Costa", "pos": "DEF", "club": "Villarreal"},
            {"name": "Roberto Lopes", "pos": "DEF", "club": "Shamrock Rovers"},
            {"name": "Moreira", "pos": "DEF", "club": "Columbus Crew"},
            {"name": "Pina", "pos": "DEF", "club": "Trabzonspor"},
            {"name": "Pires", "pos": "DEF", "club": "Seinajoki"},
            {"name": "Stopira", "pos": "DEF", "club": "Torreense"},
            {"name": "Arcanjo", "pos": "MID", "club": "Vitoria Guimaraes"},
            {"name": "Duarte", "pos": "MID", "club": "Ludogorets"},
            {"name": "Laros Duarte", "pos": "MID", "club": "Puskas Akademia"},
            {"name": "Joao Paulo", "pos": "MID", "club": "FCSB"},
            {"name": "Monteiro", "pos": "MID", "club": "PEC Zwolle"},
            {"name": "Pina", "pos": "MID", "club": "Krasnodar"},
            {"name": "Semedo", "pos": "MID", "club": "Farense"},
            {"name": "Benchimol", "pos": "ATT", "club": "Akron Togliatti"},
            {"name": "Cabral", "pos": "ATT", "club": "Estrela Amadora"},
            {"name": "Livramento", "pos": "ATT", "club": "Casa Pia"},
            {"name": "Mendes", "pos": "ATT", "club": "Igdir"},
            {"name": "Da Costa", "pos": "ATT", "club": "Basaksehir"},
            {"name": "Rodrigues", "pos": "ATT", "club": "Apollon Limassol"},
            {"name": "Semedo", "pos": "ATT", "club": "Omonia Nicosia"},
            {"name": "Varela", "pos": "ATT", "club": "Maccabi Tel Aviv"},
        ],
    },
    "Francia": {
        "group": "I",
        "coach": "Didier Deschamps",
        "players": [
            {"name": "Maignan", "pos": "GK", "club": "Milan"},
            {"name": "Risser", "pos": "GK", "club": "Lens"},
            {"name": "Samba", "pos": "GK", "club": "Rennes"},
            {"name": "Digne", "pos": "DEF", "club": "Everton"},
            {"name": "Gusto", "pos": "DEF", "club": "Chelsea"},
            {"name": "Lucas Hernandez", "pos": "DEF", "club": "PSG"},
            {"name": "Theo Hernandez", "pos": "DEF", "club": "Al-Hilal"},
            {"name": "Konate", "pos": "DEF", "club": "Liverpool"},
            {"name": "Kounde", "pos": "DEF", "club": "Barcellona"},
            {"name": "Lacroix", "pos": "DEF", "club": "Crystal Palace"},
            {"name": "Saliba", "pos": "DEF", "club": "Arsenal"},
            {"name": "Upamecano", "pos": "DEF", "club": "Bayern Monaco"},
            {"name": "Kante", "pos": "MID", "club": "Fenerbahce"},
            {"name": "Kone", "pos": "MID", "club": "Roma"},
            {"name": "Rabiot", "pos": "MID", "club": "Milan"},
            {"name": "Tchouameni", "pos": "MID", "club": "Real Madrid"},
            {"name": "Zaire-Emery", "pos": "MID", "club": "PSG"},
            {"name": "Akliouche", "pos": "ATT", "club": "Monaco"},
            {"name": "Barcola", "pos": "ATT", "club": "PSG"},
            {"name": "Cherki", "pos": "ATT", "club": "Manchester City"},
            {"name": "Dembele", "pos": "ATT", "club": "PSG"},
            {"name": "Doue", "pos": "ATT", "club": "PSG"},
            {"name": "Mateta", "pos": "ATT", "club": "Crystal Palace"},
            {"name": "Mbappe", "pos": "ATT", "club": "Real Madrid"},
            {"name": "Olise", "pos": "ATT", "club": "Bayern Monaco"},
            {"name": "Thuram", "pos": "ATT", "club": "Inter"},
        ],
    },
    "Austria": {
        "group": "J",
        "coach": "Ralf Rangnick",
        "players": [
            {"name": "Pentz", "pos": "GK", "club": "Brondby"},
            {"name": "Schlager", "pos": "GK", "club": "Salisburgo"},
            {"name": "Wiegele", "pos": "GK", "club": "Viktoria Plzen"},
            {"name": "Affengruber", "pos": "DEF", "club": "Elche"},
            {"name": "Alaba", "pos": "DEF", "club": "Real Madrid"},
            {"name": "Danso", "pos": "DEF", "club": "Tottenham"},
            {"name": "Friedl", "pos": "DEF", "club": "Werder Brema"},
            {"name": "Lienhart", "pos": "DEF", "club": "Friburgo"},
            {"name": "Mwene", "pos": "DEF", "club": "Mainz"},
            {"name": "Posch", "pos": "DEF", "club": "Mainz"},
            {"name": "Prass", "pos": "DEF", "club": "Hoffenheim"},
            {"name": "Svoboda", "pos": "DEF", "club": "Venezia"},
            {"name": "Baumgartner", "pos": "MID", "club": "Lipsia"},
            {"name": "Chukwuemeka", "pos": "MID", "club": "Borussia Dortmund"},
            {"name": "Grillitsch", "pos": "MID", "club": "Braga"},
            {"name": "Laimer", "pos": "MID", "club": "Bayern Monaco"},
            {"name": "Sabitzer", "pos": "MID", "club": "Borussia Dortmund"},
            {"name": "Schlager", "pos": "MID", "club": "Lipsia"},
            {"name": "Schmid", "pos": "MID", "club": "Werder Brema"},
            {"name": "Schopf", "pos": "MID", "club": "Wolfsberger"},
            {"name": "Seiwald", "pos": "MID", "club": "Lipsia"},
            {"name": "Wanner", "pos": "MID", "club": "PSV Eindhoven"},
            {"name": "Wimmer", "pos": "MID", "club": "Wolfsburg"},
            {"name": "Arnautovic", "pos": "ATT", "club": "Stella Rossa"},
            {"name": "Gregoritsch", "pos": "ATT", "club": "Augsburg"},
            {"name": "Kalajdzic", "pos": "ATT", "club": "LASK"},
        ],
    },
    "Portogallo": {
        "group": "K",
        "coach": "Roberto Martinez",
        "players": [
            {"name": "Diogo Costa", "pos": "GK", "club": "Porto"},
            {"name": "Sa", "pos": "GK", "club": "Wolverhampton"},
            {"name": "Rui Silva", "pos": "GK", "club": "Sporting CP"},
            {"name": "Velho", "pos": "GK", "club": "Genclerbirligi"},
            {"name": "Dalot", "pos": "DEF", "club": "Manchester United"},
            {"name": "Matheus Nunes", "pos": "DEF", "club": "Manchester City"},
            {"name": "Semedo", "pos": "DEF", "club": "Fenerbahce"},
            {"name": "Cancelo", "pos": "DEF", "club": "Barcellona"},
            {"name": "Nuno Mendes", "pos": "DEF", "club": "PSG"},
            {"name": "Goncalo Inacio", "pos": "DEF", "club": "Sporting CP"},
            {"name": "Veiga", "pos": "DEF", "club": "Villarreal"},
            {"name": "Ruben Dias", "pos": "DEF", "club": "Manchester City"},
            {"name": "Araujo", "pos": "DEF", "club": "Benfica"},
            {"name": "Ruben Neves", "pos": "MID", "club": "Al-Hilal"},
            {"name": "Samu Costa", "pos": "MID", "club": "Maiorca"},
            {"name": "Joao Neves", "pos": "MID", "club": "PSG"},
            {"name": "Vitinha", "pos": "MID", "club": "PSG"},
            {"name": "Bruno Fernandes", "pos": "MID", "club": "Manchester United"},
            {"name": "Bernardo Silva", "pos": "MID", "club": "Manchester City"},
            {"name": "Joao Felix", "pos": "ATT", "club": "Al-Nassr"},
            {"name": "Trincao", "pos": "ATT", "club": "Sporting CP"},
            {"name": "Conceicao", "pos": "ATT", "club": "Juventus"},
            {"name": "Pedro Neto", "pos": "ATT", "club": "Chelsea"},
            {"name": "Leao", "pos": "ATT", "club": "Milan"},
            {"name": "Guedes", "pos": "ATT", "club": "Real Sociedad"},
            {"name": "Ramos", "pos": "ATT", "club": "PSG"},
            {"name": "Cristiano Ronaldo", "pos": "ATT", "club": "Al-Nassr"},
        ],
    },
    "Rep. Dem. Congo": {
        "group": "K",
        "coach": "Sebastien Desabre",
        "players": [
            {"name": "Fayulu", "pos": "GK", "club": "Noah"},
            {"name": "Mpasi", "pos": "GK", "club": "Le Havre"},
            {"name": "Epolo", "pos": "GK", "club": "Standard Liegi"},
            {"name": "Wan-Bissaka", "pos": "DEF", "club": "West Ham United"},
            {"name": "Kalulu", "pos": "DEF", "club": "Limassol"},
            {"name": "Kayembe", "pos": "DEF", "club": "Genk"},
            {"name": "Masuaku", "pos": "DEF", "club": "Lens"},
            {"name": "Kapuadi", "pos": "DEF", "club": "Widzew Lodz"},
            {"name": "Bushiri", "pos": "DEF", "club": "Hibernian"},
            {"name": "Tuanzebe", "pos": "DEF", "club": "Burnley"},
            {"name": "Mbemba", "pos": "DEF", "club": "Lille"},
            {"name": "Batubinsika", "pos": "DEF", "club": "Larissa"},
            {"name": "Sadiki", "pos": "MID", "club": "Sunderland"},
            {"name": "Moutoussamy", "pos": "MID", "club": "Atromitos"},
            {"name": "E. Kayembe", "pos": "MID", "club": "Watford"},
            {"name": "Mukau", "pos": "MID", "club": "Lille"},
            {"name": "Pickel", "pos": "MID", "club": "Espanyol"},
            {"name": "Mbuku", "pos": "MID", "club": "Montpellier"},
            {"name": "Cipenga", "pos": "MID", "club": "Castellon"},
            {"name": "Elia", "pos": "MID", "club": "Alanyaspor"},
            {"name": "Bongonda", "pos": "MID", "club": "Spartak Mosca"},
            {"name": "Kakuta", "pos": "MID", "club": "AEL"},
            {"name": "Mayele", "pos": "ATT", "club": "Pyramids"},
            {"name": "Bakambu", "pos": "ATT", "club": "Betis"},
            {"name": "Banza", "pos": "ATT", "club": "Al-Jazira"},
            {"name": "Wissa", "pos": "ATT", "club": "Newcastle"},
        ],
    },
    "Germania": {
        "group": "E",
        "coach": "Julian Nagelsmann",
        "players": [
            {"name": "Manuel Neuer", "pos": "GK", "club": "Bayern Monaco"},
            {"name": "Oliver Baumann", "pos": "GK", "club": "Hoffenheim"},
            {"name": "Alexander Nübel", "pos": "GK", "club": "Stoccarda"},
            {"name": "Waldemar Anton", "pos": "DEF", "club": "Borussia Dortmund"},
            {"name": "Nathaniel Brown", "pos": "DEF", "club": "Eintracht Francoforte"},
            {"name": "Joshua Kimmich", "pos": "DEF", "club": "Bayern Monaco"},
            {"name": "David Raum", "pos": "DEF", "club": "Lipsia"},
            {"name": "Antonio Rüdiger", "pos": "DEF", "club": "Real Madrid"},
            {"name": "Malick Thiaw", "pos": "DEF", "club": "Newcastle"},
            {"name": "Nico Schlotterbeck", "pos": "DEF", "club": "Borussia Dortmund"},
            {"name": "Jonathan Tah", "pos": "DEF", "club": "Bayern Monaco"},
            {"name": "Leon Goretzka", "pos": "MID", "club": "Bayern Monaco"},
            {"name": "Pascal Groß", "pos": "MID", "club": "Brighton"},
            {"name": "Felix Nmecha", "pos": "MID", "club": "Borussia Dortmund"},
            {"name": "Angelo Stiller", "pos": "MID", "club": "Stoccarda"},
            {"name": "Jamie Leweling", "pos": "MID", "club": "Stoccarda"},
            {"name": "Lennart Karl", "pos": "MID", "club": "Bayern Monaco"},
            {"name": "Aleksandar Pavlović", "pos": "MID", "club": "Bayern Monaco"},
            {"name": "Jamal Musiala", "pos": "ATT", "club": "Bayern Monaco"},
            {"name": "Nadiem Amiri", "pos": "ATT", "club": "Mainz"},
            {"name": "Kai Havertz", "pos": "ATT", "club": "Arsenal"},
            {"name": "Leroy Sané", "pos": "ATT", "club": "Galatasaray"},
            {"name": "Deniz Undav", "pos": "ATT", "club": "Stoccarda"},
            {"name": "Maximilian Beier", "pos": "ATT", "club": "Borussia Dortmund"},
            {"name": "Nick Woltemade", "pos": "ATT", "club": "Newcastle"},
            {"name": "Florian Wirtz", "pos": "ATT", "club": "Liverpool"},
        ],
    },
    "Croazia": {
        "group": "L",
        "coach": "Zlatko Dalic",
        "players": [
            {"name": "Livakovic", "pos": "GK", "club": "Dinamo Zagabria"},
            {"name": "Kotarski", "pos": "GK", "club": "Copenaghen"},
            {"name": "Pandur", "pos": "GK", "club": "Hull City"},
            {"name": "Gvardiol", "pos": "DEF", "club": "Manchester City"},
            {"name": "Caleta-Car", "pos": "DEF", "club": "Real Sociedad"},
            {"name": "Sutalo", "pos": "DEF", "club": "Ajax"},
            {"name": "Stanisic", "pos": "DEF", "club": "Bayern Monaco"},
            {"name": "Pongracic", "pos": "DEF", "club": "Fiorentina"},
            {"name": "Erlic", "pos": "DEF", "club": "Midtjylland"},
            {"name": "Vuskovic", "pos": "DEF", "club": "Amburgo"},
            {"name": "Modric", "pos": "MID", "club": "Milan"},
            {"name": "Mateo Kovacic", "pos": "MID", "club": "Manchester City"},
            {"name": "Mario Pasalic", "pos": "MID", "club": "Atalanta"},
            {"name": "Vlasic", "pos": "MID", "club": "Torino"},
            {"name": "Luka Sucic", "pos": "MID", "club": "Real Sociedad"},
            {"name": "Baturina", "pos": "MID", "club": "Como"},
            {"name": "Jakic", "pos": "MID", "club": "Augsburg"},
            {"name": "Petar Sucic", "pos": "MID", "club": "Inter"},
            {"name": "Moro", "pos": "MID", "club": "Bologna"},
            {"name": "Fruk", "pos": "MID", "club": "Rijeka"},
            {"name": "Perisic", "pos": "ATT", "club": "PSV"},
            {"name": "Kramaric", "pos": "ATT", "club": "Hoffenheim"},
            {"name": "Budimir", "pos": "ATT", "club": "Osasuna"},
            {"name": "Marco Pasalic", "pos": "ATT", "club": "Orlando City"},
            {"name": "Musa", "pos": "ATT", "club": "Dallas"},
            {"name": "Matanovic", "pos": "ATT", "club": "Friburgo"},
        ],
    },
    "Egitto": {
        "group": "G",
        "coach": "Hossam Hassan",
        "players": [
            {"name": "El Shennawy", "pos": "GK", "club": "Al Ahly"},
            {"name": "Shobeir", "pos": "GK", "club": "Al Ahly"},
            {"name": "Soliman", "pos": "GK", "club": "Zamalek"},
            {"name": "Mohamed Alaa", "pos": "GK", "club": "El Gouna"},
            {"name": "Hany", "pos": "DEF", "club": "Al Ahly"},
            {"name": "Tarek Alaa", "pos": "DEF", "club": "Zed"},
            {"name": "Fathi", "pos": "DEF", "club": "Al-Wakrah"},
            {"name": "Rabia", "pos": "DEF", "club": "Al-Ain"},
            {"name": "Yasser Ibrahim", "pos": "DEF", "club": "Al Ahly"},
            {"name": "Abdelmaguid", "pos": "DEF", "club": "Zamalek"},
            {"name": "Abdelmonem", "pos": "DEF", "club": "Nizza"},
            {"name": "Fattouh", "pos": "DEF", "club": "Zamalek"},
            {"name": "Hafez", "pos": "DEF", "club": "Pyramids"},
            {"name": "Attia", "pos": "MID", "club": "Al Ahly"},
            {"name": "Lasheen", "pos": "MID", "club": "Pyramids"},
            {"name": "Dunga", "pos": "MID", "club": "Al-Najma"},
            {"name": "Saber", "pos": "MID", "club": "Zed"},
            {"name": "Zizo", "pos": "MID", "club": "Al Ahly"},
            {"name": "Trezeguet", "pos": "MID", "club": "Al Ahly"},
            {"name": "Ashour", "pos": "MID", "club": "Al Ahly"},
            {"name": "Ziko", "pos": "MID", "club": "Pyramids"},
            {"name": "Adel", "pos": "MID", "club": "Nordsjaelland"},
            {"name": "Hassan", "pos": "MID", "club": "Real Oviedo"},
            {"name": "Salah", "pos": "MID", "club": "Liverpool"},
            {"name": "Marmoush", "pos": "ATT", "club": "Manchester City"},
            {"name": "Abdullah", "pos": "ATT", "club": "Enppi"},
            {"name": "Abdelkarim", "pos": "ATT", "club": "Barcellona"},
        ],
    },
    "Senegal": {
        "group": "I",
        "coach": "Pape Thiaw",
        "players": [
            {"name": "Edouard Mendy", "pos": "GK", "club": "Al-Ahli"},
            {"name": "Diaw", "pos": "GK", "club": "Le Havre"},
            {"name": "Yehvann Diouf", "pos": "GK", "club": "Nizza"},
            {"name": "Diatta", "pos": "DEF", "club": "Monaco"},
            {"name": "Antoine Mendy", "pos": "DEF", "club": "Nizza"},
            {"name": "Koulibaly", "pos": "DEF", "club": "Al-Hilal"},
            {"name": "El Hadji Malick Diouf", "pos": "DEF", "club": "West Ham"},
            {"name": "Mamadou Sarr", "pos": "DEF", "club": "Chelsea"},
            {"name": "Niakhate", "pos": "DEF", "club": "Lione"},
            {"name": "Mbow", "pos": "DEF", "club": "Paris FC"},
            {"name": "Seck", "pos": "DEF", "club": "Maccabi Haifa"},
            {"name": "Jakobs", "pos": "DEF", "club": "Galatasaray"},
            {"name": "Ilay Camara", "pos": "DEF", "club": "Anderlecht"},
            {"name": "Idrissa Gueye", "pos": "MID", "club": "Everton"},
            {"name": "Pape Gueye", "pos": "MID", "club": "Villarreal"},
            {"name": "Lamine Camara", "pos": "MID", "club": "Monaco"},
            {"name": "Diarra", "pos": "MID", "club": "Sunderland"},
            {"name": "Ciss", "pos": "MID", "club": "Rayo Vallecano"},
            {"name": "Pape Matar Sarr", "pos": "MID", "club": "Tottenham"},
            {"name": "Sapoko Ndiaye", "pos": "MID", "club": "Bayern Monaco"},
            {"name": "Mane", "pos": "ATT", "club": "Al-Nassr"},
            {"name": "Ismaila Sarr", "pos": "ATT", "club": "Crystal Palace"},
            {"name": "Iliman Ndiaye", "pos": "ATT", "club": "Everton"},
            {"name": "Diao", "pos": "ATT", "club": "Como"},
            {"name": "Mbaye", "pos": "ATT", "club": "PSG"},
            {"name": "Jackson", "pos": "ATT", "club": "Bayern Monaco"},
            {"name": "Dieng", "pos": "ATT", "club": "Lorient"},
            {"name": "Cherif Ndiaye", "pos": "ATT", "club": "Samsunspor"},
        ],
    },
    "Norvegia": {
        "group": "I",
        "coach": "Stale Solbakken",
        "players": [
            {"name": "Nyland", "pos": "GK", "club": "Siviglia"},
            {"name": "Selvik", "pos": "GK", "club": "Watford"},
            {"name": "Tangvik", "pos": "GK", "club": "Amburgo"},
            {"name": "Vassbakk Ajer", "pos": "DEF", "club": "Brentford"},
            {"name": "Bjorkan", "pos": "DEF", "club": "Bodo/Glimt"},
            {"name": "Falchener", "pos": "DEF", "club": "Viking FK"},
            {"name": "Langas", "pos": "DEF", "club": "Derby County"},
            {"name": "Heggem", "pos": "DEF", "club": "Bologna"},
            {"name": "Pedersen", "pos": "DEF", "club": "Torino"},
            {"name": "Ryerson", "pos": "DEF", "club": "Borussia Dortmund"},
            {"name": "Moller Wolfe", "pos": "DEF", "club": "Wolverhampton"},
            {"name": "Ostigard", "pos": "DEF", "club": "Genoa"},
            {"name": "Aasgaard", "pos": "MID", "club": "Rangers"},
            {"name": "Aursnes", "pos": "MID", "club": "Benfica"},
            {"name": "Berg", "pos": "MID", "club": "Bodo/Glimt"},
            {"name": "Berge", "pos": "MID", "club": "Fulham"},
            {"name": "Bobb", "pos": "MID", "club": "Fulham"},
            {"name": "Hauge", "pos": "MID", "club": "Bodo/Glimt"},
            {"name": "Nusa", "pos": "MID", "club": "Lipsia"},
            {"name": "Schjelderup", "pos": "MID", "club": "Benfica"},
            {"name": "Thorsby", "pos": "MID", "club": "Cremonese"},
            {"name": "Thorstvedt", "pos": "MID", "club": "Sassuolo"},
            {"name": "Odegaard", "pos": "MID", "club": "Arsenal"},
            {"name": "Haaland", "pos": "ATT", "club": "Manchester City"},
            {"name": "Strand Larsen", "pos": "ATT", "club": "Crystal Palace"},
            {"name": "Sorloth", "pos": "ATT", "club": "Atletico Madrid"},
        ],
    },
    "Inghilterra": {
        "group": "L",
        "coach": "Thomas Tuchel",
        "players": [
            {"name": "Pickford", "pos": "GK", "club": "Everton"},
            {"name": "Dean Henderson", "pos": "GK", "club": "Crystal Palace"},
            {"name": "Trafford", "pos": "GK", "club": "Manchester City"},
            {"name": "James", "pos": "DEF", "club": "Chelsea"},
            {"name": "Konsa", "pos": "DEF", "club": "Aston Villa"},
            {"name": "Quansah", "pos": "DEF", "club": "Bayer Leverkusen"},
            {"name": "Stones", "pos": "DEF", "club": "Manchester City"},
            {"name": "Guehi", "pos": "DEF", "club": "Manchester City"},
            {"name": "Burn", "pos": "DEF", "club": "Newcastle"},
            {"name": "O'Reilly", "pos": "DEF", "club": "Manchester City"},
            {"name": "Spence", "pos": "DEF", "club": "Tottenham"},
            {"name": "Livramento", "pos": "DEF", "club": "Newcastle"},
            {"name": "Rice", "pos": "MID", "club": "Arsenal"},
            {"name": "Anderson", "pos": "MID", "club": "Nottingham Forest"},
            {"name": "Mainoo", "pos": "MID", "club": "Manchester United"},
            {"name": "Jordan Henderson", "pos": "MID", "club": "Brentford"},
            {"name": "Rogers", "pos": "MID", "club": "Aston Villa"},
            {"name": "Bellingham", "pos": "MID", "club": "Real Madrid"},
            {"name": "Eze", "pos": "MID", "club": "Arsenal"},
            {"name": "Kane", "pos": "ATT", "club": "Bayern Monaco"},
            {"name": "Toney", "pos": "ATT", "club": "Al-Ahli"},
            {"name": "Watkins", "pos": "ATT", "club": "Aston Villa"},
            {"name": "Saka", "pos": "ATT", "club": "Arsenal"},
            {"name": "Rashford", "pos": "ATT", "club": "Barcelona"},
            {"name": "Gordon", "pos": "ATT", "club": "Newcastle"},
            {"name": "Madueke", "pos": "ATT", "club": "Arsenal"},
        ],
    },
}


def import_squads():
    """Import all WC squads into the database, preserving TM enrichment data."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)

    # Preserve existing enrichment data (TM IDs are negative)
    existing_enrichment = {}
    try:
        for row in conn.execute("""
            SELECT player_name, country, player_id, matched_rating, matched_stats_json
            FROM wc_squads
            WHERE player_id IS NOT NULL
        """).fetchall():
            key = (row[0], row[1])  # (player_name, country)
            existing_enrichment[key] = {
                "player_id": row[2],
                "matched_rating": row[3],
                "matched_stats_json": row[4],
            }
    except Exception:
        pass

    # Clear and re-import
    conn.execute("DELETE FROM wc_squads")

    total = 0
    for country, data in WC_SQUADS.items():
        group = data["group"]
        coach = data["coach"]
        for p in data["players"]:
            conn.execute("""
                INSERT OR REPLACE INTO wc_squads
                (country, wc_group, coach, player_name, position, club)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (country, group, coach, p["name"], p["pos"], p["club"]))
            total += 1

    conn.commit()
    logger.info(f"✅ Importati {total} giocatori da {len(WC_SQUADS)} nazionali")

    # Match with our local DB (5 leagues)
    matched = _match_players(conn)

    # Restore TM enrichment for players not matched by local DB
    restored = 0
    for row in conn.execute("""
        SELECT id, player_name, country FROM wc_squads
        WHERE player_id IS NULL
    """).fetchall():
        key = (row[1], row[2])
        if key in existing_enrichment:
            e = existing_enrichment[key]
            conn.execute("""
                UPDATE wc_squads
                SET player_id = ?, matched_rating = ?, matched_stats_json = ?
                WHERE id = ?
            """, (e["player_id"], e["matched_rating"], e["matched_stats_json"], row[0]))
            restored += 1

    if restored:
        conn.commit()
        logger.info(f"♻️ Ripristinati {restored} enrichment TM precedenti")

    conn.close()
    return {"imported": total, "countries": len(WC_SQUADS), "matched": matched + restored}


def _match_players(conn):
    """Match WC squad players to our player_info + player_stats_cache."""
    cursor = conn.cursor()

    # Load all our players with stats
    our_players = {}
    cursor.execute("""
        SELECT pi.player_id, pi.name, pi.team_name, pi.position_id,
               psc.rating,
               psc.stats_json
        FROM player_info pi
        LEFT JOIN player_stats_cache psc
            ON pi.player_id = psc.player_id
        WHERE pi.name IS NOT NULL
    """)
    for row in cursor.fetchall():
        pid, name, team, pos_id, rating, stats_json = row
        if not name:
            continue
        name_low = name.lower().strip()
        our_players[name_low] = {
            "player_id": pid,
            "name": name,
            "team": team,
            "position_id": pos_id,
            "rating": rating or 0,
            "stats_json": stats_json,
        }

    # Build multiple indexes for flexible matching
    surname_index = {}
    # Normalized index: remove hyphens, lowercase, for Asian name matching
    norm_index = {}  # normalized_name -> data
    for name_low, data in our_players.items():
        parts = name_low.split()
        if parts:
            surname = parts[-1]
            if surname not in surname_index:
                surname_index[surname] = []
            surname_index[surname].append((name_low, data))
        # Normalized: no hyphens, no dots
        norm = name_low.replace("-", "").replace(".", "")
        norm_index[norm] = data
        # Also index reversed name (for Korean/Japanese/etc: "Min-Jae Kim" ↔ "Kim Min-jae")
        if len(parts) >= 2:
            reversed_name = " ".join(parts[1:]) + " " + parts[0]
            rev_norm = reversed_name.replace("-", "").replace(".", "")
            if rev_norm not in norm_index:
                norm_index[rev_norm] = data

    # Match each WC player
    wc_players = cursor.execute("SELECT id, player_name, club FROM wc_squads").fetchall()

    matched = 0
    for wc_id, wc_name, wc_club in wc_players:
        wc_low = wc_name.lower().strip()

        # 1. Exact match
        match = our_players.get(wc_low)

        # 2. Normalized match (handles hyphens + name order)
        #    "Kim Min-jae" → "kim minjae" matches "minjae kim" → "Min-Jae Kim"
        if not match:
            wc_norm = wc_low.replace("-", "").replace(".", "")
            match = norm_index.get(wc_norm)
            # Also try reversed WC name normalized
            if not match:
                wc_parts = wc_norm.split()
                if len(wc_parts) >= 2:
                    wc_reversed = " ".join(wc_parts[1:]) + " " + wc_parts[0]
                    match = norm_index.get(wc_reversed)

        # 3. Surname match (only for multi-word names to avoid false positives)
        if not match:
            wc_parts = wc_low.replace(".", "").split()
            wc_surname = wc_parts[-1] if wc_parts else wc_low
            # Skip very short surnames (<=3 chars) that cause false matches (Sa, Da, etc.)
            if len(wc_surname) > 3:
                candidates = surname_index.get(wc_surname, [])

                if len(candidates) == 1:
                    # Verify: single-word WC name must exactly equal surname
                    # Multi-word WC name: check initial matches
                    cand_name, cand_data = candidates[0]
                    if len(wc_parts) == 1:
                        match = cand_data
                    else:
                        # Verify first initial matches to avoid "Petar Sucic" → "Luka Sucic"
                        cand_parts = cand_name.split()
                        wc_initial = wc_parts[0][0]
                        if any(p[0] == wc_initial for p in cand_parts):
                            match = cand_data
                elif len(candidates) > 1:
                    # Multiple matches — try first initial
                    wc_initial = wc_parts[0][0] if len(wc_parts) > 1 else ""
                    for cand_name, cand_data in candidates:
                        cand_parts = cand_name.split()
                        if cand_parts and cand_parts[0][0] == wc_initial:
                            match = cand_data
                            break

        # 4. Surname match with first part as surname (Asian names: "Kim Min-jae" → surname=Kim)
        if not match:
            wc_parts = wc_low.replace(".", "").split()
            if len(wc_parts) >= 2:
                wc_first = wc_parts[0]  # Could be surname for Asian names
                candidates = surname_index.get(wc_first, [])
                if len(candidates) == 1:
                    # Verify given name overlaps to avoid false matches
                    cand_name, cand_data = candidates[0]
                    wc_given = wc_parts[-1].replace("-", "") if len(wc_parts) >= 2 else ""
                    if wc_given and wc_given in cand_name.replace("-", ""):
                        match = cand_data
                elif len(candidates) > 1:
                    # Try matching given name
                    wc_given = wc_parts[-1].replace("-", "") if len(wc_parts) >= 2 else ""
                    for cand_name, cand_data in candidates:
                        if wc_given and wc_given in cand_name.replace("-", ""):
                            match = cand_data
                            break

        # 5. Substring match (e.g., "Vinicius Jr" in "vinicius jose")
        #    Only if WC name is >8 chars and covers >60% of the DB name
        #    to avoid "Elliot" matching "Elliot Anderson", "Da Costa" matching "Danny da Costa"
        if not match and len(wc_low) > 8:
            for our_name, our_data in our_players.items():
                if wc_low in our_name and len(wc_low) / len(our_name) > 0.6:
                    match = our_data
                    break
                if our_name in wc_low and len(our_name) / len(wc_low) > 0.6:
                    match = our_data
                    break

        if match:
            cursor.execute("""
                UPDATE wc_squads
                SET player_id = ?, matched_rating = ?, matched_stats_json = ?
                WHERE id = ?
            """, (match["player_id"], match["rating"], match["stats_json"], wc_id))
            matched += 1

    conn.commit()
    logger.info(f"🔗 Matchati {matched}/{len(wc_players)} giocatori con il nostro DB")
    return matched


def get_squad(country: str) -> dict:
    """Get a WC squad with matched stats."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT * FROM wc_squads WHERE country = ? ORDER BY
            CASE position
                WHEN 'GK' THEN 1
                WHEN 'DEF' THEN 2
                WHEN 'MID' THEN 3
                WHEN 'ATT' THEN 4
            END, matched_rating DESC
    """, (country,)).fetchall()
    conn.close()

    if not rows:
        return {"error": f"Nazione '{country}' non trovata"}

    players = []
    matched_count = 0
    total_rating = 0
    rated_count = 0

    for r in rows:
        p = {
            "name": r["player_name"],
            "position": r["position"],
            "club": r["club"],
            "matched": r["player_id"] is not None,
            "rating": r["matched_rating"],
        }

        # Parse stats if available
        if r["matched_stats_json"]:
            try:
                stats = json.loads(r["matched_stats_json"])
                p["goals"] = stats.get("goals", 0)
                p["assists"] = stats.get("assists", 0)
                p["appearances"] = stats.get("appearances", 0)
                p["fouls"] = stats.get("fouls_committed", 0)
                p["tackles"] = stats.get("tackles", 0)
                p["shots"] = stats.get("shots_total", 0)
                p["minutes"] = stats.get("minutes_played", 0)
            except Exception:
                pass

        if p["matched"]:
            matched_count += 1
        if p.get("rating") and p["rating"] > 0:
            total_rating += p["rating"]
            rated_count += 1

        players.append(p)

    return {
        "country": rows[0]["country"],
        "group": rows[0]["wc_group"],
        "coach": rows[0]["coach"],
        "players": players,
        "total": len(players),
        "matched": matched_count,
        "avg_rating": round(total_rating / rated_count, 1) if rated_count > 0 else None,
    }


def get_all_squads_summary() -> list[dict]:
    """Get summary of all WC squads with strength rankings."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    countries = conn.execute("""
        SELECT country, wc_group, coach,
               COUNT(*) as total_players,
               SUM(CASE WHEN player_id IS NOT NULL THEN 1 ELSE 0 END) as matched,
               AVG(CASE WHEN matched_rating > 0 THEN matched_rating END) as avg_rating,
               SUM(CASE WHEN matched_stats_json IS NOT NULL
                   THEN JSON_EXTRACT(matched_stats_json, '$.goals') ELSE 0 END) as total_goals
        FROM wc_squads
        GROUP BY country
        ORDER BY avg_rating DESC
    """).fetchall()
    conn.close()

    return [
        {
            "country": r["country"],
            "group": r["wc_group"],
            "coach": r["coach"],
            "total_players": r["total_players"],
            "matched": r["matched"],
            "avg_rating": round(r["avg_rating"], 1) if r["avg_rating"] else None,
            "total_goals": r["total_goals"] or 0,
        }
        for r in countries
    ]


def get_top_scorers(limit: int = 20) -> list[dict]:
    """Get top WC scorer candidates based on club stats."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT player_name, country, club, position, matched_rating,
               JSON_EXTRACT(matched_stats_json, '$.goals') as goals,
               JSON_EXTRACT(matched_stats_json, '$.appearances') as apps,
               JSON_EXTRACT(matched_stats_json, '$.shots_total') as shots,
               JSON_EXTRACT(matched_stats_json, '$.assists') as assists
        FROM wc_squads
        WHERE matched_stats_json IS NOT NULL
          AND position IN ('ATT', 'MID')
          AND JSON_EXTRACT(matched_stats_json, '$.goals') > 0
        ORDER BY CAST(JSON_EXTRACT(matched_stats_json, '$.goals') AS REAL) /
                 MAX(CAST(JSON_EXTRACT(matched_stats_json, '$.appearances') AS REAL), 1) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    return [
        {
            "player": r["player_name"],
            "country": r["country"],
            "club": r["club"],
            "position": r["position"],
            "rating": r["matched_rating"],
            "goals": r["goals"],
            "appearances": r["apps"],
            "goals_per_match": round(r["goals"] / max(r["apps"], 1), 2),
            "shots": r["shots"],
            "assists": r["assists"],
        }
        for r in rows
    ]


def get_top_card_candidates(limit: int = 20) -> list[dict]:
    """Get top WC card candidates based on club stats."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT player_name, country, club, position, matched_rating,
               JSON_EXTRACT(matched_stats_json, '$.fouls_committed') as fouls,
               JSON_EXTRACT(matched_stats_json, '$.tackles') as tackles,
               JSON_EXTRACT(matched_stats_json, '$.appearances') as apps
        FROM wc_squads
        WHERE matched_stats_json IS NOT NULL
          AND position IN ('DEF', 'MID')
          AND JSON_EXTRACT(matched_stats_json, '$.appearances') >= 5
        ORDER BY (CAST(JSON_EXTRACT(matched_stats_json, '$.fouls_committed') AS REAL) +
                  CAST(JSON_EXTRACT(matched_stats_json, '$.tackles') AS REAL) * 0.3) /
                 MAX(CAST(JSON_EXTRACT(matched_stats_json, '$.appearances') AS REAL), 1) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    return [
        {
            "player": r["player_name"],
            "country": r["country"],
            "club": r["club"],
            "position": r["position"],
            "rating": r["matched_rating"],
            "fouls": r["fouls"],
            "tackles": r["tackles"],
            "appearances": r["apps"],
            "fouls_per_match": round(r["fouls"] / max(r["apps"], 1), 2),
            "tackles_per_match": round(r["tackles"] / max(r["apps"], 1), 2),
        }
        for r in rows
    ]


def enrich_unmatched_players():
    """Search Transfermarkt for WC players not in our DB and fetch their stats.

    Uses TM search + ceapi/player/{id}/performance to get goals, assists,
    appearances, cards for ALL leagues worldwide.
    """
    import re
    import time
    import requests
    import urllib3
    urllib3.disable_warnings()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    # Get all unmatched WC players
    unmatched = conn.execute("""
        SELECT id, player_name, club, position, country
        FROM wc_squads
        WHERE player_id IS NULL
    """).fetchall()

    if not unmatched:
        logger.info("✅ Tutti i giocatori WC già matchati — niente da fare")
        return {"searched": 0, "found": 0}

    logger.info(f"🔍 Enrichment TM: {len(unmatched)} giocatori WC senza stats...")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    found = 0
    errors = 0

    for i, row in enumerate(unmatched):
        wc_id = row["id"]
        name = row["player_name"]
        club = row["club"]
        position = row["position"]

        if i > 0 and i % 20 == 0:
            logger.info(f"   ... progresso: {i}/{len(unmatched)} cercati, {found} trovati")

        try:
            # 1. Search on Transfermarkt by name
            search_url = f"https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={name.replace(' ', '+')}"
            r = requests.get(search_url, headers=headers, timeout=10, verify=False)
            if r.status_code != 200:
                time.sleep(2)
                continue

            # Find player profile link
            links = re.findall(r'(/[a-z\-]+/profil/spieler/(\d+))', r.text)
            if not links:
                # Log first failure to help debug (captcha? redirect?)
                if found == 0 and i < 5:
                    snippet = r.text[:300].replace('\n', ' ')
                    logger.info(f"   ⚠️ Debug {name}: status={r.status_code}, len={len(r.text)}, start={snippet[:150]}...")
                time.sleep(1.5)
                continue

            tm_id = links[0][1]
            time.sleep(1.5)

            # 2. Get performance stats via TM internal API
            perf_url = f"https://www.transfermarkt.com/ceapi/player/{tm_id}/performance"
            r2 = requests.get(perf_url, headers=headers, timeout=10, verify=False)
            if r2.status_code != 200 or not r2.json():
                time.sleep(1.5)
                continue

            data = r2.json()

            # Aggregate stats across all competitions
            total_apps = sum(c.get("gamesPlayed", 0) for c in data)
            total_goals = sum(c.get("goalsScored", 0) for c in data)
            total_assists = sum(c.get("assists", 0) for c in data)
            total_yellows = sum(c.get("yellowCards", 0) for c in data)
            total_reds = sum(c.get("redCards", 0) for c in data)

            if total_apps == 0:
                time.sleep(1.5)
                continue

            # Build stats dict compatible with our format
            stats = {
                "goals": total_goals,
                "assists": total_assists,
                "appearances": total_apps,
                "fouls_committed": total_yellows * 3,  # Estimate: ~3 fouls per yellow
                "tackles": 0,
                "shots_total": total_goals * 3 if position == "ATT" else total_goals * 4,  # Rough estimate
                "shots_on_target": total_goals,  # Minimum
                "minutes_played": int(total_apps * 75),  # Estimate avg 75 min/game
                "fouls_drawn": 0,
                "interceptions": 0,
                "blocks": 0,
                "clearances": 0,
                "yellow_cards": total_yellows,
                "red_cards": total_reds,
                "source": "transfermarkt",
                "tm_id": int(tm_id),
            }

            # Calculate a simple rating based on position
            if position == "GK":
                # GK rating based on appearances (clean sheets unknown)
                rating = min(70, 45 + total_apps * 0.5)
            elif position == "DEF":
                rating = min(75, 50 + total_apps * 0.3 + total_goals * 2 + total_assists * 1.5)
            elif position == "MID":
                goals_per_game = total_goals / max(total_apps, 1)
                assists_per_game = total_assists / max(total_apps, 1)
                rating = min(80, 50 + total_apps * 0.2 + total_goals * 2.5 + total_assists * 2 + goals_per_game * 15 + assists_per_game * 10)
            else:  # ATT
                goals_per_game = total_goals / max(total_apps, 1)
                rating = min(85, 50 + total_goals * 2 + total_assists * 1.5 + goals_per_game * 20)

            rating = round(rating, 1)
            stats_json = json.dumps(stats)

            # Use negative TM ID to distinguish from Sportmonks player IDs
            fake_player_id = -int(tm_id)

            conn.execute("""
                UPDATE wc_squads
                SET player_id = ?, matched_rating = ?, matched_stats_json = ?
                WHERE id = ?
            """, (fake_player_id, rating, stats_json, wc_id))
            found += 1

            logger.info(f"   ✅ {name} → TM#{tm_id} | {total_apps}app {total_goals}g {total_assists}a | R={rating}")
            time.sleep(1.5)

        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.warning(f"   ⚠ Errore per {name}: {e}")
            time.sleep(2)

    conn.commit()
    conn.close()

    logger.info(f"✅ Enrichment TM completato: {found}/{len(unmatched)} nuovi match trovati ({errors} errori)")
    return {"searched": len(unmatched), "found": found, "errors": errors}


# ══════════════════════════════════════════════════════════════
# MATCH CALENDAR — FIFA World Cup 2026
# All times in Italian timezone (CEST)
# ══════════════════════════════════════════════════════════════

WC_CALENDAR = [
    # ── GRUPPO A ──
    {"phase": "Gironi", "group": "A", "matchday": 1, "home": "Messico", "away": "Sudafrica", "date": "2026-06-11", "time": "21:00", "venue": "Mexico City Stadium"},
    {"phase": "Gironi", "group": "A", "matchday": 1, "home": "Corea del Sud", "away": "Repubblica Ceca", "date": "2026-06-12", "time": "04:00", "venue": "Estadio Guadalajara"},
    {"phase": "Gironi", "group": "A", "matchday": 2, "home": "Repubblica Ceca", "away": "Sudafrica", "date": "2026-06-18", "time": "18:00", "venue": "Atlanta Stadium"},
    {"phase": "Gironi", "group": "A", "matchday": 2, "home": "Messico", "away": "Corea del Sud", "date": "2026-06-19", "time": "03:00", "venue": "Estadio Guadalajara"},
    {"phase": "Gironi", "group": "A", "matchday": 3, "home": "Sudafrica", "away": "Corea del Sud", "date": "2026-06-25", "time": "03:00", "venue": "Estadio Monterrey"},
    {"phase": "Gironi", "group": "A", "matchday": 3, "home": "Repubblica Ceca", "away": "Messico", "date": "2026-06-25", "time": "03:00", "venue": "Mexico City Stadium"},

    # ── GRUPPO B ──
    {"phase": "Gironi", "group": "B", "matchday": 1, "home": "Canada", "away": "Bosnia", "date": "2026-06-12", "time": "21:00", "venue": "Toronto Stadium"},
    {"phase": "Gironi", "group": "B", "matchday": 1, "home": "Svizzera", "away": "Qatar", "date": "2026-06-13", "time": "21:00", "venue": "San Francisco Bay Area Stadium"},
    {"phase": "Gironi", "group": "B", "matchday": 2, "home": "Svizzera", "away": "Bosnia", "date": "2026-06-18", "time": "21:00", "venue": "Los Angeles Stadium"},
    {"phase": "Gironi", "group": "B", "matchday": 2, "home": "Canada", "away": "Qatar", "date": "2026-06-19", "time": "00:00", "venue": "BC Place Vancouver"},
    {"phase": "Gironi", "group": "B", "matchday": 3, "home": "Svizzera", "away": "Canada", "date": "2026-06-24", "time": "21:00", "venue": "BC Place Vancouver"},
    {"phase": "Gironi", "group": "B", "matchday": 3, "home": "Bosnia", "away": "Qatar", "date": "2026-06-24", "time": "21:00", "venue": "Seattle Stadium"},

    # ── GRUPPO C ──
    {"phase": "Gironi", "group": "C", "matchday": 1, "home": "Brasile", "away": "Marocco", "date": "2026-06-14", "time": "00:00", "venue": "New York New Jersey Stadium"},
    {"phase": "Gironi", "group": "C", "matchday": 1, "home": "Haiti", "away": "Scozia", "date": "2026-06-14", "time": "03:00", "venue": "Boston Stadium"},
    {"phase": "Gironi", "group": "C", "matchday": 2, "home": "Scozia", "away": "Marocco", "date": "2026-06-20", "time": "00:00", "venue": "Boston Stadium"},
    {"phase": "Gironi", "group": "C", "matchday": 2, "home": "Brasile", "away": "Haiti", "date": "2026-06-20", "time": "03:00", "venue": "Philadelphia Stadium"},
    {"phase": "Gironi", "group": "C", "matchday": 3, "home": "Marocco", "away": "Haiti", "date": "2026-06-25", "time": "00:00", "venue": "Atlanta Stadium"},
    {"phase": "Gironi", "group": "C", "matchday": 3, "home": "Scozia", "away": "Brasile", "date": "2026-06-25", "time": "00:00", "venue": "Miami Stadium"},

    # ── GRUPPO D ──
    {"phase": "Gironi", "group": "D", "matchday": 1, "home": "USA", "away": "Paraguay", "date": "2026-06-13", "time": "03:00", "venue": "Los Angeles Stadium"},
    {"phase": "Gironi", "group": "D", "matchday": 1, "home": "Australia", "away": "Turchia", "date": "2026-06-13", "time": "06:00", "venue": "BC Place Vancouver"},
    {"phase": "Gironi", "group": "D", "matchday": 2, "home": "Turchia", "away": "Paraguay", "date": "2026-06-19", "time": "06:00", "venue": "San Francisco Bay Area Stadium"},
    {"phase": "Gironi", "group": "D", "matchday": 2, "home": "USA", "away": "Australia", "date": "2026-06-19", "time": "21:00", "venue": "Seattle Stadium"},
    {"phase": "Gironi", "group": "D", "matchday": 3, "home": "Turchia", "away": "USA", "date": "2026-06-26", "time": "04:00", "venue": "Los Angeles Stadium"},
    {"phase": "Gironi", "group": "D", "matchday": 3, "home": "Paraguay", "away": "Australia", "date": "2026-06-26", "time": "04:00", "venue": "San Francisco Bay Area Stadium"},

    # ── GRUPPO E ──
    {"phase": "Gironi", "group": "E", "matchday": 1, "home": "Germania", "away": "Curacao", "date": "2026-06-14", "time": "19:00", "venue": "Houston Stadium"},
    {"phase": "Gironi", "group": "E", "matchday": 1, "home": "Costa d'Avorio", "away": "Ecuador", "date": "2026-06-14", "time": "22:00", "venue": "Philadelphia Stadium"},
    {"phase": "Gironi", "group": "E", "matchday": 2, "home": "Germania", "away": "Costa d'Avorio", "date": "2026-06-20", "time": "22:00", "venue": "Toronto Stadium"},
    {"phase": "Gironi", "group": "E", "matchday": 2, "home": "Ecuador", "away": "Curacao", "date": "2026-06-21", "time": "02:00", "venue": "Kansas City Stadium"},
    {"phase": "Gironi", "group": "E", "matchday": 3, "home": "Curacao", "away": "Costa d'Avorio", "date": "2026-06-25", "time": "22:00", "venue": "Philadelphia Stadium"},
    {"phase": "Gironi", "group": "E", "matchday": 3, "home": "Ecuador", "away": "Germania", "date": "2026-06-25", "time": "22:00", "venue": "New York New Jersey Stadium"},

    # ── GRUPPO F ──
    {"phase": "Gironi", "group": "F", "matchday": 1, "home": "Olanda", "away": "Giappone", "date": "2026-06-14", "time": "22:00", "venue": "Dallas Stadium"},
    {"phase": "Gironi", "group": "F", "matchday": 1, "home": "Svezia", "away": "Tunisia", "date": "2026-06-15", "time": "04:00", "venue": "Estadio Monterrey"},
    {"phase": "Gironi", "group": "F", "matchday": 2, "home": "Tunisia", "away": "Giappone", "date": "2026-06-20", "time": "06:00", "venue": "Estadio Monterrey"},
    {"phase": "Gironi", "group": "F", "matchday": 2, "home": "Olanda", "away": "Svezia", "date": "2026-06-20", "time": "19:00", "venue": "Houston Stadium"},
    {"phase": "Gironi", "group": "F", "matchday": 3, "home": "Tunisia", "away": "Olanda", "date": "2026-06-26", "time": "01:00", "venue": "Kansas City Stadium"},
    {"phase": "Gironi", "group": "F", "matchday": 3, "home": "Giappone", "away": "Svezia", "date": "2026-06-26", "time": "01:00", "venue": "Dallas Stadium"},

    # ── GRUPPO G ──
    {"phase": "Gironi", "group": "G", "matchday": 1, "home": "Belgio", "away": "Egitto", "date": "2026-06-15", "time": "21:00", "venue": "Seattle Stadium"},
    {"phase": "Gironi", "group": "G", "matchday": 1, "home": "Iran", "away": "Nuova Zelanda", "date": "2026-06-16", "time": "03:00", "venue": "Los Angeles Stadium"},
    {"phase": "Gironi", "group": "G", "matchday": 2, "home": "Belgio", "away": "Iran", "date": "2026-06-21", "time": "21:00", "venue": "Los Angeles Stadium"},
    {"phase": "Gironi", "group": "G", "matchday": 2, "home": "Nuova Zelanda", "away": "Egitto", "date": "2026-06-22", "time": "03:00", "venue": "BC Place Vancouver"},
    {"phase": "Gironi", "group": "G", "matchday": 3, "home": "Nuova Zelanda", "away": "Belgio", "date": "2026-06-27", "time": "05:00", "venue": "BC Place Vancouver"},
    {"phase": "Gironi", "group": "G", "matchday": 3, "home": "Egitto", "away": "Iran", "date": "2026-06-27", "time": "05:00", "venue": "Seattle Stadium"},

    # ── GRUPPO H ──
    {"phase": "Gironi", "group": "H", "matchday": 1, "home": "Spagna", "away": "Capo Verde", "date": "2026-06-15", "time": "18:00", "venue": "Atlanta Stadium"},
    {"phase": "Gironi", "group": "H", "matchday": 1, "home": "Arabia Saudita", "away": "Uruguay", "date": "2026-06-16", "time": "00:00", "venue": "Miami Stadium"},
    {"phase": "Gironi", "group": "H", "matchday": 2, "home": "Spagna", "away": "Arabia Saudita", "date": "2026-06-21", "time": "18:00", "venue": "Atlanta Stadium"},
    {"phase": "Gironi", "group": "H", "matchday": 2, "home": "Uruguay", "away": "Capo Verde", "date": "2026-06-22", "time": "00:00", "venue": "Miami Stadium"},
    {"phase": "Gironi", "group": "H", "matchday": 3, "home": "Capo Verde", "away": "Arabia Saudita", "date": "2026-06-27", "time": "02:00", "venue": "Houston Stadium"},
    {"phase": "Gironi", "group": "H", "matchday": 3, "home": "Uruguay", "away": "Spagna", "date": "2026-06-27", "time": "02:00", "venue": "Estadio Guadalajara"},

    # ── GRUPPO I ──
    {"phase": "Gironi", "group": "I", "matchday": 1, "home": "Francia", "away": "Senegal", "date": "2026-06-16", "time": "21:00", "venue": "New York New Jersey Stadium"},
    {"phase": "Gironi", "group": "I", "matchday": 1, "home": "Iraq", "away": "Norvegia", "date": "2026-06-17", "time": "00:00", "venue": "Boston Stadium"},
    {"phase": "Gironi", "group": "I", "matchday": 2, "home": "Francia", "away": "Iraq", "date": "2026-06-22", "time": "23:00", "venue": "Philadelphia Stadium"},
    {"phase": "Gironi", "group": "I", "matchday": 2, "home": "Norvegia", "away": "Senegal", "date": "2026-06-23", "time": "02:00", "venue": "New York New Jersey Stadium"},
    {"phase": "Gironi", "group": "I", "matchday": 3, "home": "Norvegia", "away": "Francia", "date": "2026-06-26", "time": "21:00", "venue": "Boston Stadium"},
    {"phase": "Gironi", "group": "I", "matchday": 3, "home": "Senegal", "away": "Iraq", "date": "2026-06-26", "time": "21:00", "venue": "Toronto Stadium"},

    # ── GRUPPO J ──
    {"phase": "Gironi", "group": "J", "matchday": 1, "home": "Austria", "away": "Giordania", "date": "2026-06-16", "time": "06:00", "venue": "San Francisco Bay Area Stadium"},
    {"phase": "Gironi", "group": "J", "matchday": 1, "home": "Argentina", "away": "Algeria", "date": "2026-06-17", "time": "03:00", "venue": "Kansas City Stadium"},
    {"phase": "Gironi", "group": "J", "matchday": 2, "home": "Argentina", "away": "Austria", "date": "2026-06-22", "time": "19:00", "venue": "Dallas Stadium"},
    {"phase": "Gironi", "group": "J", "matchday": 2, "home": "Giordania", "away": "Algeria", "date": "2026-06-23", "time": "05:00", "venue": "San Francisco Bay Area Stadium"},
    {"phase": "Gironi", "group": "J", "matchday": 3, "home": "Algeria", "away": "Austria", "date": "2026-06-28", "time": "04:00", "venue": "Kansas City Stadium"},
    {"phase": "Gironi", "group": "J", "matchday": 3, "home": "Giordania", "away": "Argentina", "date": "2026-06-28", "time": "04:00", "venue": "Dallas Stadium"},

    # ── GRUPPO K ──
    {"phase": "Gironi", "group": "K", "matchday": 1, "home": "Portogallo", "away": "Rep. Dem. Congo", "date": "2026-06-17", "time": "19:00", "venue": "Houston Stadium"},
    {"phase": "Gironi", "group": "K", "matchday": 1, "home": "Uzbekistan", "away": "Colombia", "date": "2026-06-18", "time": "04:00", "venue": "Mexico City Stadium"},
    {"phase": "Gironi", "group": "K", "matchday": 2, "home": "Portogallo", "away": "Uzbekistan", "date": "2026-06-23", "time": "19:00", "venue": "Houston Stadium"},
    {"phase": "Gironi", "group": "K", "matchday": 2, "home": "Colombia", "away": "Rep. Dem. Congo", "date": "2026-06-24", "time": "04:00", "venue": "Estadio Guadalajara"},
    {"phase": "Gironi", "group": "K", "matchday": 3, "home": "Colombia", "away": "Portogallo", "date": "2026-06-28", "time": "01:30", "venue": "Miami Stadium"},
    {"phase": "Gironi", "group": "K", "matchday": 3, "home": "Rep. Dem. Congo", "away": "Uzbekistan", "date": "2026-06-28", "time": "01:30", "venue": "Atlanta Stadium"},

    # ── GRUPPO L ──
    {"phase": "Gironi", "group": "L", "matchday": 1, "home": "Inghilterra", "away": "Croazia", "date": "2026-06-17", "time": "22:00", "venue": "Dallas Stadium"},
    {"phase": "Gironi", "group": "L", "matchday": 1, "home": "Ghana", "away": "Panama", "date": "2026-06-18", "time": "01:00", "venue": "Toronto Stadium"},
    {"phase": "Gironi", "group": "L", "matchday": 2, "home": "Inghilterra", "away": "Ghana", "date": "2026-06-23", "time": "22:00", "venue": "Boston Stadium"},
    {"phase": "Gironi", "group": "L", "matchday": 2, "home": "Panama", "away": "Croazia", "date": "2026-06-24", "time": "01:00", "venue": "Toronto Stadium"},
    {"phase": "Gironi", "group": "L", "matchday": 3, "home": "Panama", "away": "Inghilterra", "date": "2026-06-27", "time": "23:00", "venue": "New York New Jersey Stadium"},
    {"phase": "Gironi", "group": "L", "matchday": 3, "home": "Croazia", "away": "Ghana", "date": "2026-06-27", "time": "23:00", "venue": "Philadelphia Stadium"},

    # ══════════════════════════════════════════════════════════════
    # KNOCKOUT — Sedicesimi (Round of 32)
    # ══════════════════════════════════════════════════════════════
    {"phase": "Sedicesimi", "match_id": 73, "home": "2ª Gruppo A", "away": "2ª Gruppo B", "date": "2026-06-28", "time": "21:00", "venue": "Los Angeles Stadium"},
    {"phase": "Sedicesimi", "match_id": 74, "home": "1ª Gruppo E", "away": "3ª A/B/C/D/F", "date": "2026-06-29", "time": "22:30", "venue": "Boston Stadium"},
    {"phase": "Sedicesimi", "match_id": 75, "home": "1ª Gruppo F", "away": "2ª Gruppo C", "date": "2026-06-30", "time": "03:00", "venue": "Estadio Monterrey"},
    {"phase": "Sedicesimi", "match_id": 76, "home": "1ª Gruppo C", "away": "2ª Gruppo F", "date": "2026-06-29", "time": "19:00", "venue": "Houston Stadium"},
    {"phase": "Sedicesimi", "match_id": 77, "home": "1ª Gruppo I", "away": "3ª C/D/F/G/H", "date": "2026-06-30", "time": "23:00", "venue": "New York New Jersey Stadium"},
    {"phase": "Sedicesimi", "match_id": 78, "home": "2ª Gruppo A", "away": "2ª Gruppo I", "date": "2026-06-30", "time": "19:00", "venue": "Dallas Stadium"},
    {"phase": "Sedicesimi", "match_id": 79, "home": "1ª Gruppo A", "away": "3ª C/E/F/H/I", "date": "2026-07-01", "time": "03:00", "venue": "Mexico City Stadium"},
    {"phase": "Sedicesimi", "match_id": 80, "home": "1ª Gruppo L", "away": "3ª E/H/I/J/K", "date": "2026-07-01", "time": "18:00", "venue": "Atlanta Stadium"},
    {"phase": "Sedicesimi", "match_id": 81, "home": "1ª Gruppo D", "away": "3ª B/E/F/I/J", "date": "2026-07-02", "time": "02:00", "venue": "San Francisco Bay Area Stadium"},
    {"phase": "Sedicesimi", "match_id": 82, "home": "1ª Gruppo G", "away": "3ª A/E/H/I/J", "date": "2026-07-01", "time": "22:00", "venue": "Seattle Stadium"},
    {"phase": "Sedicesimi", "match_id": 83, "home": "2ª Gruppo K", "away": "2ª Gruppo L", "date": "2026-07-03", "time": "01:00", "venue": "Toronto Stadium"},
    {"phase": "Sedicesimi", "match_id": 84, "home": "1ª Gruppo H", "away": "2ª Gruppo J", "date": "2026-07-02", "time": "21:00", "venue": "Los Angeles Stadium"},
    {"phase": "Sedicesimi", "match_id": 85, "home": "1ª Gruppo B", "away": "3ª E/F/G/I/J", "date": "2026-07-03", "time": "05:00", "venue": "BC Place Vancouver"},
    {"phase": "Sedicesimi", "match_id": 86, "home": "1ª Gruppo J", "away": "2ª Gruppo H", "date": "2026-07-04", "time": "00:00", "venue": "Miami Stadium"},
    {"phase": "Sedicesimi", "match_id": 87, "home": "1ª Gruppo K", "away": "3ª D/E/I/J/L", "date": "2026-07-04", "time": "03:30", "venue": "Kansas City Stadium"},
    {"phase": "Sedicesimi", "match_id": 88, "home": "2ª Gruppo D", "away": "2ª Gruppo G", "date": "2026-07-03", "time": "20:00", "venue": "Dallas Stadium"},

    # ══════════════════════════════════════════════════════════════
    # KNOCKOUT — Ottavi (Round of 16)
    # ══════════════════════════════════════════════════════════════
    {"phase": "Ottavi", "match_id": 89, "home": "V. partita 74", "away": "V. partita 77", "date": "2026-07-04", "time": "23:00", "venue": "Philadelphia Stadium"},
    {"phase": "Ottavi", "match_id": 90, "home": "V. partita 73", "away": "V. partita 75", "date": "2026-07-04", "time": "19:00", "venue": "Houston Stadium"},
    {"phase": "Ottavi", "match_id": 91, "home": "V. partita 76", "away": "V. partita 78", "date": "2026-07-05", "time": "22:00", "venue": "New York New Jersey Stadium"},
    {"phase": "Ottavi", "match_id": 92, "home": "V. partita 79", "away": "V. partita 80", "date": "2026-07-06", "time": "02:00", "venue": "Mexico City Stadium"},
    {"phase": "Ottavi", "match_id": 93, "home": "V. partita 83", "away": "V. partita 84", "date": "2026-07-06", "time": "21:00", "venue": "Dallas Stadium"},
    {"phase": "Ottavi", "match_id": 94, "home": "V. partita 81", "away": "V. partita 82", "date": "2026-07-07", "time": "02:00", "venue": "Seattle Stadium"},
    {"phase": "Ottavi", "match_id": 95, "home": "V. partita 86", "away": "V. partita 88", "date": "2026-07-07", "time": "18:00", "venue": "Atlanta Stadium"},
    {"phase": "Ottavi", "match_id": 96, "home": "V. partita 85", "away": "V. partita 87", "date": "2026-07-07", "time": "22:00", "venue": "BC Place Vancouver"},

    # ══════════════════════════════════════════════════════════════
    # KNOCKOUT — Quarti di finale
    # ══════════════════════════════════════════════════════════════
    {"phase": "Quarti", "match_id": 97, "home": "V. partita 89", "away": "V. partita 90", "date": "2026-07-09", "time": "22:00", "venue": "Boston Stadium"},
    {"phase": "Quarti", "match_id": 98, "home": "V. partita 93", "away": "V. partita 94", "date": "2026-07-10", "time": "21:00", "venue": "Los Angeles Stadium"},
    {"phase": "Quarti", "match_id": 99, "home": "V. partita 91", "away": "V. partita 92", "date": "2026-07-11", "time": "23:00", "venue": "Miami Stadium"},
    {"phase": "Quarti", "match_id": 100, "home": "V. partita 95", "away": "V. partita 96", "date": "2026-07-12", "time": "03:00", "venue": "Kansas City Stadium"},

    # ══════════════════════════════════════════════════════════════
    # KNOCKOUT — Semifinali
    # ══════════════════════════════════════════════════════════════
    {"phase": "Semifinali", "match_id": 101, "home": "V. partita 97", "away": "V. partita 98", "date": "2026-07-14", "time": "21:00", "venue": "Dallas Stadium"},
    {"phase": "Semifinali", "match_id": 102, "home": "V. partita 99", "away": "V. partita 100", "date": "2026-07-15", "time": "21:00", "venue": "Atlanta Stadium"},

    # ══════════════════════════════════════════════════════════════
    # FINALI
    # ══════════════════════════════════════════════════════════════
    {"phase": "Finale 3°/4°", "match_id": 103, "home": "P. partita 101", "away": "P. partita 102", "date": "2026-07-18", "time": "23:00", "venue": "Miami Stadium"},
    {"phase": "Finale", "match_id": 104, "home": "V. partita 101", "away": "V. partita 102", "date": "2026-07-19", "time": "21:00", "venue": "New York New Jersey Stadium"},
]


def get_group_standings() -> dict:
    """Calculate group standings from WC_CALENDAR results + team ratings.

    Returns dict keyed by group letter, each value is a list of team dicts
    sorted by pts > gd > gf, with fields:
        country, pts, w, d, l, gf, ga, gd, avg_rating, played
    """
    from collections import defaultdict

    # Init standings for all groups
    ALL_GROUPS = {
        "A": ["Messico", "Sudafrica", "Corea del Sud", "Repubblica Ceca"],
        "B": ["Canada", "Bosnia", "Qatar", "Svizzera"],
        "C": ["Brasile", "Marocco", "Haiti", "Scozia"],
        "D": ["USA", "Paraguay", "Australia", "Turchia"],
        "E": ["Germania", "Curacao", "Costa d'Avorio", "Ecuador"],
        "F": ["Olanda", "Giappone", "Svezia", "Tunisia"],
        "G": ["Belgio", "Egitto", "Iran", "Nuova Zelanda"],
        "H": ["Spagna", "Capo Verde", "Arabia Saudita", "Uruguay"],
        "I": ["Francia", "Senegal", "Iraq", "Norvegia"],
        "J": ["Argentina", "Algeria", "Austria", "Giordania"],
        "K": ["Portogallo", "Rep. Dem. Congo", "Uzbekistan", "Colombia"],
        "L": ["Inghilterra", "Croazia", "Ghana", "Panama"],
    }

    standings = {}
    for g, teams in ALL_GROUPS.items():
        standings[g] = {
            t: {"country": t, "pts": 0, "w": 0, "d": 0, "l": 0,
                "gf": 0, "ga": 0, "gd": 0, "played": 0, "avg_rating": None}
            for t in teams
        }

    # Process group stage results from calendar
    for m in WC_CALENDAR:
        if m["phase"] != "Gironi":
            continue
        if "score_home" not in m or m["score_home"] is None:
            continue  # Match not played yet
        g = m["group"]
        home, away = m["home"], m["away"]
        sh, sa = m["score_home"], m["score_away"]

        if home in standings.get(g, {}):
            standings[g][home]["played"] += 1
            standings[g][home]["gf"] += sh
            standings[g][home]["ga"] += sa
        if away in standings.get(g, {}):
            standings[g][away]["played"] += 1
            standings[g][away]["gf"] += sa
            standings[g][away]["ga"] += sh

        if sh > sa:  # Home win
            if home in standings.get(g, {}):
                standings[g][home]["w"] += 1
                standings[g][home]["pts"] += 3
            if away in standings.get(g, {}):
                standings[g][away]["l"] += 1
        elif sa > sh:  # Away win
            if away in standings.get(g, {}):
                standings[g][away]["w"] += 1
                standings[g][away]["pts"] += 3
            if home in standings.get(g, {}):
                standings[g][home]["l"] += 1
        else:  # Draw
            if home in standings.get(g, {}):
                standings[g][home]["d"] += 1
                standings[g][home]["pts"] += 1
            if away in standings.get(g, {}):
                standings[g][away]["d"] += 1
                standings[g][away]["pts"] += 1

    # Add avg_rating from DB
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        for row in conn.execute("""
            SELECT country, AVG(CASE WHEN matched_rating > 0 THEN matched_rating END) as avg_rating
            FROM wc_squads GROUP BY country
        """).fetchall():
            if row["avg_rating"]:
                r = round(row["avg_rating"], 1)
                for g in standings:
                    if row["country"] in standings[g]:
                        standings[g][row["country"]]["avg_rating"] = r
        conn.close()
    except Exception:
        pass

    # Build sorted result
    result = {}
    for g, teams_dict in sorted(standings.items()):
        team_list = list(teams_dict.values())
        for t in team_list:
            t["gd"] = t["gf"] - t["ga"]
        # Sort: pts desc, gd desc, gf desc
        team_list.sort(key=lambda t: (t["pts"], t["gd"], t["gf"]), reverse=True)
        # If no matches played, sort by rating instead
        if all(t["played"] == 0 for t in team_list):
            team_list.sort(key=lambda t: t["avg_rating"] or 0, reverse=True)
        result[g] = team_list

    return result


def get_calendar(phase_filter: str = None) -> list[dict]:
    """Get WC calendar, optionally filtered by phase."""
    from datetime import datetime as dt

    matches = WC_CALENDAR
    if phase_filter:
        matches = [m for m in matches if m["phase"] == phase_filter]

    # Enrich group stage matches with team ratings from DB
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        ratings = {}
        for row in conn.execute("""
            SELECT country, AVG(CASE WHEN matched_rating > 0 THEN matched_rating END) as avg_rating
            FROM wc_squads GROUP BY country
        """).fetchall():
            if row["avg_rating"]:
                ratings[row["country"]] = round(row["avg_rating"], 1)
        conn.close()
    except Exception:
        ratings = {}

    result = []
    today = dt.now().strftime("%Y-%m-%d")
    for m in matches:
        entry = dict(m)
        entry["home_rating"] = ratings.get(m["home"])
        entry["away_rating"] = ratings.get(m["away"])
        # Status: past / today / upcoming
        if m["date"] < today:
            entry["status"] = "past"
        elif m["date"] == today:
            entry["status"] = "today"
        else:
            entry["status"] = "upcoming"
        result.append(entry)

    return result


def get_calendar_by_date() -> dict:
    """Get calendar grouped by date for display."""
    cal = get_calendar()
    by_date = {}
    for m in cal:
        d = m["date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(m)
    return dict(sorted(by_date.items()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = import_squads()
    print(f"\n{'='*50}")
    print(f"Import completato: {result}")

    print(f"\n{'='*50}")
    print("RANKING NAZIONALI (per rating medio):")
    for s in get_all_squads_summary():
        r = s['avg_rating'] or 0
        bar = "█" * int(r / 5) if r else ""
        print(f"  {s['country']:25s} | Gruppo {s['group']} | Rating: {r:5.1f} {bar} | {s['matched']}/{s['total_players']} matchati | {s['total_goals']}g")

    print(f"\n{'='*50}")
    print("TOP 15 CANDIDATI MARCATORI:")
    for s in get_top_scorers(15):
        print(f"  {s['player']:25s} ({s['country']:15s}) | {s['goals']}g in {s['appearances']} app ({s['goals_per_match']}/g) | {s['shots']} tiri | Rating: {s['rating']}")

    print(f"\n{'='*50}")
    print("TOP 15 CANDIDATI CARTELLINI:")
    for c in get_top_card_candidates(15):
        print(f"  {c['player']:25s} ({c['country']:15s}) | {c['fouls']} falli ({c['fouls_per_match']}/g) | {c['tackles']} tackles ({c['tackles_per_match']}/g)")
