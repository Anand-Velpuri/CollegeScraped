import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urljoin
from typing import List, Optional, Dict, Any

from bs4 import BeautifulSoup
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from httpx import AsyncClient, Limits, Timeout
from cachetools import TTLCache

from fastapi.middleware.cors import CORSMiddleware
from fastapi_proxiedheadersmiddleware import ProxiedHeadersMiddleware

# --- CONFIGURATION ---
BASE_URL = "https://rguktong.ac.in/"
PROFILE_API = urljoin(BASE_URL, "profiles/profile_details.php")
DEPARTMENTS = ["CSE", "CIVIL", "ECE", "EEE", "ME", "MATHEMATICS", "PHYSICS", "CHEMISTRY", "IT", "BIOLOGY", "ENGLISH", "LIB", "MANAGEMENT", "PED", "TELUGU", "YOGA"]

# Cache: max 200 entries, 1-hour expiration
cache = TTLCache(maxsize=200, ttl=3600)

class AppState:
    client: AsyncClient = None

state = AppState()

# --- HELPER UTILS ---
def get_abs_url(path):
    if not path or path == "#": return None
    return urljoin(BASE_URL, path) if not path.startswith('http') else path

# --- UNIVERSAL PARSERS ---

async def parse_deep_faculty(client: AsyncClient, email: str):
    """Deep Bio POST Fetcher"""
    try:
        resp = await client.post(PROFILE_API, data={"email": email}, timeout=10.0)
        if resp.status_code != 200: return {}
        p_soup = BeautifulSoup(resp.text, 'lxml')
        details = {}
        for div in p_soup.find_all('div', id=lambda x: x and x.startswith('content-')):
            key = div.get('id').replace('content-', '').replace('_', ' ').title()
            items = [li.get_text(strip=True) for li in div.find_all('li')]
            details[key] = items if items else div.get_text(strip=True)
        return details
    except: return {"error": "Deep fetch failed"}

def parse_generic_table(html):
    """Universal logic for News, Tenders, Careers tables"""
    soup = BeautifulSoup(html, 'lxml')
    results = []
    table = soup.find('table')
    if table:
        for row in table.find_all('tr')[1:]:
            cols = row.find_all('td')
            if len(cols) >= 2:
                results.append({
                    "title": cols[0].get_text(strip=True),
                    "date": cols[1].get_text(strip=True),
                    "links": [{"text": a.get_text(strip=True), "url": get_abs_url(a['href'])} for a in row.find_all('a', href=True)]
                })
    return results

# --- API LIFECYCLE ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Performance Tuning: High max connections for massive parallel POSTing
    state.client = AsyncClient(
        limits=Limits(max_connections=150, max_keepalive_connections=30),
        timeout=Timeout(30.0),
        follow_redirects=True
    )
    yield
    await state.client.aclose()

app = FastAPI(title="RGUKT Master API", lifespan=lifespan)


# Proxy headers handling
app.add_middleware(ProxiedHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"], # Allow all headers
)

async def get_http(): return state.client

# --- ALL ENDPOINTS ---

@app.get("/api/home")
async def home_api(client: AsyncClient = Depends(get_http)):
    if "home" in cache: return cache["home"]
    resp = await client.get(BASE_URL)
    soup = BeautifulSoup(resp.text, 'lxml')
    data = {
        "announcements": [{"text": a.get_text(strip=True), "link": get_abs_url(a['href'])} for a in soup.find_all('marquee')[0].find_all('a')] if soup.find('marquee') else [],
        "stats": [{"label": s.find_next('p').get_text(strip=True), "value": s['data-purecounter-end']} for s in soup.find_all('span', class_='purecounter')],
        "images": [get_abs_url(img['src']) for img in soup.find_all('img') if img.get('src')]
    }
    cache["home"] = data
    return data

@app.get("/api/institute/{page}")
async def institute_info(page: str, client: AsyncClient = Depends(get_http)):
    """Handles: aboutrgukt, campuslife, edusys, govcouncil, rtiinfo, scst"""
    if page in cache: return cache[page]
    resp = await client.get(f"{BASE_URL}instituteinfo.php?data={page}")
    soup = BeautifulSoup(resp.text, 'lxml')
    
    # Generic structured text parser
    sections = []
    content_area = soup.find('div', class_='rgukt-content') or soup.find('div', class_='main-data')
    if content_area:
        headings = content_area.find_all(['h1', 'h2', 'h3'], class_=['heading-primary', 'heading-secondary', 'heading-teriatiry'])
        for h in headings:
            content = []
            curr = h.next_sibling
            while curr and curr.name not in ['h1', 'h2', 'h3']:
                if curr.name in ['p', 'ul', 'li']: content.append(curr.get_text(strip=True))
                curr = curr.next_sibling
            sections.append({"title": h.get_text(strip=True), "content": content})
    
    # If it's a person-heavy page (SCST, Council), grab card data
    cards = []
    for card_div in soup.find_all('div', class_=['info-card', 'info-card-1']):
        name_tag = card_div.find(['h3', 'h2'])
        if name_tag:
            cards.append({
                "name": name_tag.get_text(strip=True),
                "photo": get_abs_url(card_div.find('img')['src']) if card_div.find('img') else None,
                "text": [p.get_text(strip=True) for p in card_div.find_all('p')]
            })

    res = {"page": page, "sections": sections, "profiles": cards}
    cache[page] = res
    return res

@app.get("/api/academics/{page}")
async def academic_records(page: str, client: AsyncClient = Depends(get_http)):
    """Handles: AcademicPrograms, AcademicCalender, AcademicRegulations, curicula"""
    if f"acad_{page}" in cache: return cache[f"acad_{page}"]
    resp = await client.get(f"{BASE_URL}instituteinfo.php?data={page}")
    soup = BeautifulSoup(resp.text, 'lxml')
    
    links = []
    content_area = soup.find('div', class_='rgukt-content')
    if content_area:
        # Group links under headings (like Curricula or Calendar)
        headers = content_area.find_all(['h1', 'h3'])
        for h in headers:
            section_links = []
            curr = h.next_sibling
            while curr and curr.name not in ['h1', 'h3']:
                if curr.name == 'a': 
                    section_links.append({"label": curr.get_text(strip=True), "url": get_abs_url(curr['href'])})
                curr = curr.next_sibling
            links.append({"header": h.get_text(strip=True), "links": section_links})
    
    cache[f"acad_{page}"] = links
    return links

@app.get("/api/departments/{dept_code}")
async def department_staff(dept_code: str, deep: bool = False, client: AsyncClient = Depends(get_http)):
    """
    Fetch staff. ?deep=true triggers parallel background bio scraping.\n
    DEPARTMENTS = [
    "CSE", "CIVIL", "ECE", "EEE", "ME", 
    "MATHEMATICS", "PHYSICS", "CHEMISTRY", "IT", "BIOLOGY",
    "ENGLISH", "LIB", "MANAGEMENT", "PED", "TELUGU", "YOGA"
    ]
    """
    cache_key = f"dept_{dept_code}_{deep}"
    if cache_key in cache: return cache[cache_key]

    resp = await client.get(f"{BASE_URL}departmentinfo.php?department={dept_code.upper()}")
    soup = BeautifulSoup(resp.text, 'lxml')
    staff = []
    seen = set()
    
    for form in soup.find_all('form', action=lambda x: x and 'profile_details.php' in x):
        email_input = form.find('input', {'name': 'email'})
        if not email_input: continue
        email = email_input.get('value')
        if email in seen: continue
        seen.add(email)
        
        card = form.find_parent('div', class_=['bg-white', 'rounded-lg'])
        if card:
            name_tag = card.find(['h5', 'h3'])
            staff.append({
                "name": name_tag.get_text(strip=True) if name_tag else "Unknown",
                "email": email,
                "photo": get_abs_url(card.find('img')['src']) if card.find('img') else None
            })

    if deep:
        # ASYNC GATHER: The secret to "Blazingly Fast"
        tasks = [parse_deep_faculty(client, s['email']) for s in staff]
        bios = await asyncio.gather(*tasks)
        for i, bio in enumerate(bios): staff[i]['bio'] = bio

    res = {"dept": dept_code, "faculties": staff}
    cache[cache_key] = res
    return res

@app.get("/api/notifications")
async def news_tenders_careers(type: str = Query("news_updates", enum=["news_updates", "tenders", "careers"]), 
                               client: AsyncClient = Depends(get_http)):
    if type in cache: return cache[type]
    resp = await client.get(f"{BASE_URL}instituteinfo.php?data={type}")
    data = parse_generic_table(resp.text)
    cache[type] = data
    return data

@app.get("/health")
async def health():
    return {"status": "online", "cache_entries": len(cache)}

@app.get("/", response_class=HTMLResponse)
def read_root():
    return """
    <html>
    <head>
        <title>RGUKT MASTER API</title>
        <style>
            body { background:#0f172a; color:#e5e7eb; font-family:sans-serif;
                   display:flex; flex-direction:column; align-items:center;
                   justify-content:center; height:100vh; }
            img { max-width:300px; border-radius:12px;
                  box-shadow:0 0 30px rgba(0,255,255,0.4); }
            a { color:#38bdf8; text-decoration:none; margin-top:12px; }
        </style>
    </head>
    <body>
        <img src="https://res.cloudinary.com/dzunpdnje/image/upload/v1770903098/bhAAi_1_hgpetz.jpg">
        <h2>RGUKT Scraped Data bhAAI</h2>
        <p>goto <code>/docs</code>for documentation</p>
        <a href="/docs" target="_blank">View Docs →</a>
    </body>
    </html>
    """