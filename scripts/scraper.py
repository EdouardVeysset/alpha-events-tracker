import os
import requests
from bs4 import BeautifulSoup

BIN_ID = os.environ.get('JSONBIN_BIN_ID')
API_KEY = os.environ.get('JSONBIN_API_KEY')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def fetch_existing_events():
    """Récupère les événements actuels depuis JSONBin"""
    url = f"https://api.jsonbin.io/v3/b/{BIN_ID}/latest"
    headers = {'X-Master-Key': API_KEY}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('record', [])
    except Exception as e:
        print(f"Erreur lors de la récupération JSONBin: {e}")
    return []

# --- FONCTIONS SPÉCIFIQUES EXISTANTES ---

def scrape_alfi():
    events = []
    try:
        res = requests.get("https://www.alfi.lu/events", headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for item in soup.select('.event-item, .card, article'):
                title_el = item.select_one('h2, h3, a.title')
                if title_el:
                    title = title_el.text.strip()
                    events.append({
                        "id": abs(hash(title)) % 1000000,
                        "title": title,
                        "type": "conference",
                        "date": "2027-06-15",
                        "time": "09:00",
                        "location": "Luxembourg",
                        "organizer": "ALFI",
                        "url": "https://www.alfi.lu",
                        "attendees": []
                    })
    except Exception as e:
        print(f"Erreur ALFI: {e}")
    return events

def scrape_abbl():
    events = []
    try:
        res = requests.get("https://abbl.lu/en/home/events", headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for item in soup.select('.event-card, .views-row'):
                title_el = item.select_one('h3, h2, a')
                if title_el and len(title_el.text.strip()) > 5:
                    title = title_el.text.strip()
                    events.append({
                        "id": abs(hash(title)) % 1000000,
                        "title": title,
                        "type": "seminar",
                        "date": "2027-05-10",
                        "time": "09:00",
                        "location": "Luxembourg",
                        "organizer": "ABBL",
                        "url": "https://abbl.lu",
                        "attendees": []
                    })
    except Exception as e:
        print(f"Erreur ABBL: {e}")
    return events

def scrape_lpea():
    events = []
    try:
        res = requests.get("https://lpea.lu/events/", headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for item in soup.select('.event, article, .fusion-post-wrapper'):
                title_el = item.select_one('h2, h3, a')
                if title_el and len(title_el.text.strip()) > 5:
                    title = title_el.text.strip()
                    events.append({
                        "id": abs(hash(title)) % 1000000,
                        "title": title,
                        "type": "conference",
                        "date": "2027-10-15",
                        "location": "Luxembourg",
                        "organizer": "LPEA",
                        "url": "https://lpea.lu",
                        "attendees": []
                    })
    except Exception as e:
        print(f"Erreur LPEA: {e}")
    return events

# --- FONCTION GÉNÉRIQUE EXTENSIVE POUR TOUTES LES AUTRES SOURCES ---

def scrape_generic(url, organizer_name, default_type="conference"):
    events = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for item in soup.select('.event-item, .card, article, .views-row, .fusion-post-wrapper, .event-listing'):
                title_el = item.select_one('h2, h3, a.title, .event-title')
                if title_el and len(title_el.text.strip()) > 5:
                    title = title_el.text.strip()
                    
                    link_el = item.select_one('a')
                    event_url = link_el['href'] if link_el and link_el.has_attr('href') else url
                    if event_url.startswith('/'):
                        base_domain = "/".join(url.split("/")[:3])
                        event_url = base_domain + event_url

                    events.append({
                        "id": abs(hash(title)) % 1000000,
                        "title": title,
                        "type": default_type,
                        "date": "2027-06-01",
                        "time": "09:00",
                        "location": "Luxembourg",
                        "organizer": organizer_name,
                        "url": event_url,
                        "attendees": []
                    })
    except Exception as e:
        print(f"Erreur scraping {organizer_name}: {e}")
    return events

def merge_events(existing, new_ones):
    """Fusionne intelligemment sans écraser les listes 'attendees'"""
    map_events = {e['id']: e for e in existing}
    
    for new_e in new_ones:
        if new_e['id'] in map_events:
            current_attendees = map_events[new_e['id']].get('attendees', [])
            map_events[new_e['id']].update(new_e)
            if current_attendees:
                map_events[new_e['id']]['attendees'] = current_attendees
        else:
            map_events[new_e['id']] = new_e
            
    return list(map_events.values())

def save_to_jsonbin(events):
    url = f"https://api.jsonbin.io/v3/b/{BIN_ID}"
    headers = {
        'Content-Type': 'application/json',
        'X-Master-Key': API_KEY
    }
    response = requests.put(url, headers=headers, json=events)
    if response.status_code == 200:
        print("Mise à jour réussie sur JSONBin !")
    else:
        print(f"Erreur sauvegarde: {response.text}")

if __name__ == "__main__":
    print("Démarrage du scraping multi-sources global...")
    existing = fetch_existing_events()
    
    all_new_events = []
    
    # 1. Vos scrapers spécifiques existants
    all_new_events.extend(scrape_alfi())
    all_new_events.extend(scrape_abbl())
    all_new_events.extend(scrape_lpea())
    
    # 2. Toutes les nouvelles sources étendues (Chambres, Associations, Law Firms, Big 4, Banques, etc.)
    extended_sources = [
        # Business chambers & networking
        ("https://www.bcc.lu/events/upcoming-events/", "BCC", "forum"),
        ("https://www.amcham.lu/events/", "AMCHAM", "forum"),
        ("https://paperjam.lu/club/agenda", "Paperjam Club", "forum"),
        ("https://www.cc.lu/agenda", "Chambre de Commerce", "seminar"),
        ("https://romlux.org/romlux-events/", "Romlux", "forum"),
        ("https://100women.org/events/", "100 Women in Finance", "conference"),
        ("https://www.futureoffinance.biz/upcoming-events", "Future of Finance", "conference"),
        
        # Industry associations & promotion
        ("https://www.luxembourgforfinance.com/en/events/upcoming-events/", "Luxembourg for Finance", "forum"),
        ("https://lsfi.lu/events", "LSFI", "seminar"),
        ("https://luxflag.org/events/", "LuxFLAG", "seminar"),
        ("https://www.ila.lu/event?tags=%5B4%5D", "ILA", "seminar"),
        ("https://www.hubfinance.com/events", "Hubfinance", "forum"),
        
        # Training providers
        ("https://creobis.eu/en/", "Creobis", "workshop"),
        ("https://www.houseoftraining.lu/en_GB/training/planning", "House of Training", "workshop"),
        ("https://www.arendt.com/training-agenda/", "Arendt Training", "workshop"),
        
        # Law firms and Big 4 / advisory
        ("https://www.arendt.com/news-insights/events/", "Arendt", "seminar"),
        ("https://elvingerhoss.lu/insights/events", "Elvinger Hoss", "seminar"),
        ("https://www.atoz.lu/events", "Atoz", "seminar"),
        ("https://www.ey.com/en_lu/events/location-events", "EY Luxembourg", "seminar"),
        ("https://www.pwc.lu/en/events.html", "PwC", "seminar"),
        ("https://kpmg.com/lu/en/home/events.html", "KPMG", "seminar"),
        ("https://www2.deloitte.com/lu/en/events/events.html", "Deloitte", "seminar"),
        ("https://www.grantthornton.lu/en/events/", "Grant Thornton", "seminar"),
        ("https://www.jtcgroup.com/events/", "JTC", "seminar"),
        
        # Banks, custodians, fintech and exchange
        ("https://lhoft.com/events/", "LHoFT", "workshop"),
        ("https://finologee.com/news/", "Finologee", "forum"),
        ("https://www.luxse.com/resources/events", "LuxSE", "forum"),
    ]
    
    for url, org, dtype in extended_sources:
        all_new_events.extend(scrape_generic(url, org, dtype))
    
    if all_new_events:
        final_events = merge_events(existing, all_new_events)
        save_to_jsonbin(final_events)
        print(f"Synchronisation globale terminée. Total événements gérés : {len(final_events)}")
    else:
        print("Aucun événement récupéré.")