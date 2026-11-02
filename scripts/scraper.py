import re
from datetime import datetime, timedelta

def extract_date_from_text(text):
    """Tente de trouver une date dans le texte de l'événement"""
    # Cherche un format AAAA-MM-JJ ou JJ/MM/AAAA
    match_iso = re.search(r'2027-\d{2}-\d{2}', text)
    if match_iso:
        return match_iso.group(0)
    
    match_slash = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](2027|27)', text)
    if match_slash:
        day, month = match_slash.group(1).zfill(2), match_slash.group(2).zfill(2)
        return f"2027-{month}-{day}"
        
    return None

def scrape_generic(url, organizer_name, default_type="conference"):
    events = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for item in soup.select('.event-item, .card, article, .views-row, .fusion-post-wrapper, .event-listing'):
                full_text = item.get_text(" ", strip=True)
                title_el = item.select_one('h2, h3, a.title, .event-title')
                
                if title_el and len(title_el.text.strip()) > 5:
                    title = title_el.text.strip()
                    
                    # Recherche d'une vraie date dans le bloc
                    event_date = extract_date_from_text(full_text)
                    
                    # Si aucune date n'est trouvée dans le texte, on ignore l'événement 
                    # pour éviter d'encombrer le dashboard avec des faux doublons en juin
                    if not event_date:
                        continue

                    link_el = item.select_one('a')
                    event_url = link_el['href'] if link_el and link_el.has_attr('href') else url
                    if event_url.startswith('/'):
                        base_domain = "/".join(url.split("/")[:3])
                        event_url = base_domain + event_url

                    events.append({
                        "id": abs(hash(title)) % 1000000,
                        "title": title,
                        "type": default_type,
                        "date": event_date,
                        "time": "09:00",
                        "location": "Luxembourg",
                        "organizer": organizer_name,
                        "url": event_url,
                        "attendees": []
                    })
    except Exception as e:
        print(f"Erreur scraping {organizer_name}: {e}")
    return events