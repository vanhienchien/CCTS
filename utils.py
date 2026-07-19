import re
import pandas as pd

def extract_core_station_code(station_str):
    if pd.isna(station_str) or not str(station_str).strip(): return ''
    text = str(station_str).strip()
    match = re.search(r'([a-zA-Z]+\d+)', text)
    return match.group(1).upper() if match else text.upper()

def parse_duration_to_hours(val):
    if pd.isna(val) or not str(val).strip(): return 0.0
    text = str(val).strip()
    days = int(re.search(r'(\d+)\s*day\(s\)', text).group(1)) if re.search(r'(\d+)\s*day\(s\)', text) else 0
    hours = int(re.search(r'(\d+)\s*h', text).group(1)) if re.search(r'(\d+)\s*h', text) else 0
    mins = int(re.search(r'(\d+)\s*min', text).group(1)) if re.search(r'(\d+)\s*min', text) else 0
    return (days * 24) + hours + (mins / 60)