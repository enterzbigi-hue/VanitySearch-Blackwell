import ctypes
import json
import os
import random
import subprocess
import sys
import time

# --- KONFIGURACJA ---
EXE_PATH = "VanitySearch.exe"
INPUT_FILE = "puzzle71.txt"      # plik z adresem celu (jeden adres na linię)
OUTPUT_FILE = "znalezione.txt"
RANGE_BITS = 42
CHUNK_SIZE = 1 << RANGE_BITS     # 2^42 kluczy w jednym bloku

# Zakres ogólny (Puzzle 71)
GLOBAL_START = 0x400000000000000000   # 2^70
GLOBAL_END   = 0x7FFFFFFFFFFFFFFFFF   # 2^71 - 1

# Plik bazy danych zapisujący sprawdzone zakresy
DB_FILE = "checked_ranges.json"

# Własny węzeł Bitcoin Core do sprawdzania, czy adres celu nie jest już pusty
BITCOIN_CLI = r"C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe"
BITCOIN_DATADIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Bitcoin")   # domyślny katalog Bitcoin Core


def show_alert_box(message, title="BINGO! ZNALEZIONO KLUCZ!"):
  """Wyświetla systemowe okienko ostrzegawcze w Windows (MessageBox)."""
  try:
    ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x0)
  except Exception:
    pass


def db_file_for(puzzle):
  """Plik bazy dla danej zagadki; puzzle 71 zostaje w dotychczasowym checked_ranges.json."""
  return DB_FILE if puzzle == 71 else f"checked_ranges_{puzzle}.json"


def load_db(db_file=None):
  """Wczytuje zbiór sprawdzonych paczek (start, bity) z pliku JSON.

  Stary format (sama lista startów) oznaczał paczki 2^42, więc takie wpisy dostają 42 bity.
  Jeśli plik jest uszkodzony, przerywa działanie zamiast po cichu zaczynać od zera.
  """
  db_file = db_file or DB_FILE
  if not os.path.exists(db_file):
    return set()
  try:
    with open(db_file, "r") as f:
      return {(e, 42) if isinstance(e, int) else (int(e[0]), int(e[1])) for e in json.load(f)}
  except Exception as e:
    print(f"[!] Nie mogę odczytać bazy '{db_file}': {e}")
    print("[!] Napraw lub usuń ten plik ręcznie (albo przywróć z '.bak'), potem uruchom ponownie.")
    sys.exit(1)


def save_db(checked_set, db_file=None):
  """Zapisuje bazę atomowo: najpierw do pliku tymczasowego, potem podmiana.

  Dzięki temu przerwanie w trakcie zapisu nie niszczy istniejącej bazy.
  """
  db_file = db_file or DB_FILE
  tmp = db_file + ".tmp"
  with open(tmp, "w") as f:
    json.dump([list(e) for e in sorted(checked_set)], f)
    f.flush()
    os.fsync(f.fileno())
  if os.path.exists(db_file):
    try:
      os.replace(db_file, db_file + ".bak")
    except OSError:
      pass
  os.replace(tmp, db_file)


def overlaps(start, bits, checked):
  """Czy paczka [start, start + 2^bits) zachodzi na którąś ze sprawdzonych.

  Każda paczka jest wyrównana do swojego rozmiaru, więc dwie paczki albo się nie stykają,
  albo mniejsza leży w całości w większej - wystarczy porównać starty po obcięciu do większej.
  """
  for s, b in checked:
    big = max(b, bits)
    if s >> big == start >> big:
      return True
  return False


def checked_keys(checked):
  """Łączna liczba sprawdzonych kluczy."""
  return sum(1 << b for _, b in checked)


def get_random_start(checked, bits=RANGE_BITS, lo=GLOBAL_START, hi=GLOBAL_END):
  """Losuje start paczki 2^bits (wyrównany do jej rozmiaru) w [lo, hi], która nie zachodzi na sprawdzone.

  lo musi być wyrównane do 2^bits - dla zagadek (lo = 2^(n-1)) jest tak przy każdym bits <= n-1.
  """
  size = 1 << bits
  max_blocks = (hi - lo) // size

  for _ in range(10000):
    start_val = lo + random.randint(0, max_blocks) * size
    if not overlaps(start_val, bits, checked):
      return start_val

  curr = lo
  while curr <= hi - size + 1:
    if not overlaps(curr, bits, checked):
      return curr
    curr += size

  raise Exception("Wszystkie zakresy w tej puli zostały przeszukane!")


def read_addresses():
  """Zwraca listę adresów celu z pliku INPUT_FILE."""
  with open(INPUT_FILE, "r", encoding="utf-8-sig") as f:
    return [line.strip() for line in f if line.strip()]


def get_balance(addrs):
  """Sprawdza saldo adresów w UTXO własnego węzła (scantxoutset, działa też z prune).

  Zwraca saldo w BTC albo None, gdy węzła nie da się zapytać.
  Skan całego zbioru UTXO trwa zwykle od kilkudziesięciu sekund do kilku minut.
  """
  if not os.path.exists(BITCOIN_CLI):
    print(f"[!] Nie znaleziono '{BITCOIN_CLI}', pomijam sprawdzanie salda.")
    return None
  descriptors = json.dumps([f"addr({a})" for a in addrs])
  try:
    r = subprocess.run([BITCOIN_CLI, f"-datadir={BITCOIN_DATADIR}", "scantxoutset", "start", descriptors],
                       capture_output=True, text=True, timeout=1800)
  except (OSError, subprocess.TimeoutExpired) as e:
    print(f"[!] Nie udało się zapytać węzła: {e}")
    return None
  if r.returncode != 0:
    print(f"[!] Węzeł zwrócił błąd: {r.stderr.strip()}")
    return None
  try:
    return float(json.loads(r.stdout)["total_amount"])
  except (ValueError, KeyError) as e:
    print(f"[!] Nie rozumiem odpowiedzi węzła: {e}")
    return None


def target_still_funded(addrs):
  """Zwraca False tylko wtedy, gdy węzeł potwierdził, że adresy celu są puste."""
  print("[*] Sprawdzam saldo celu w węźle Bitcoin Core...")
  t0 = time.time()
  balance = get_balance(addrs)
  if balance is None:
    print("[!] Saldo nieznane, szukam dalej bez tej kontroli.")
    return True
  print(f"[*] Saldo celu: {balance:.8f} BTC (sprawdzone w {time.time() - t0:.0f} s).")
  return balance > 0


def check_files():
  """Sprawdza przed startem, czy program i plik z adresami istnieją."""
  ok = True
  if not os.path.exists(EXE_PATH):
    print(f"[!] Nie znaleziono '{EXE_PATH}'. Uruchom skrypt z folderu VanitySearch.")
    ok = False
  if not os.path.exists(INPUT_FILE):
    print(f"[!] Nie znaleziono pliku z adresami '{INPUT_FILE}'.")
    ok = False
  else:
    addrs = read_addresses()
    if not addrs:
      print(f"[!] Plik '{INPUT_FILE}' jest pusty.")
      ok = False
    else:
      print(f"[*] Szukane adresy ({len(addrs)}): {', '.join(addrs[:5])}{' ...' if len(addrs) > 5 else ''}")
  if not ok:
    sys.exit(1)


def main():
  check_files()

  checked_starts = load_db()
  print(f"[*] Załadowano bazę: {len(checked_starts)} ukończonych wcześniej zakresów.")
  print("[*] Tryb natywny: VanitySearch działa bezpośrednio, sprawdzam plik wyników po zakończeniu paczki.")

  # Sprawdzamy początkowy rozmiar pliku z wynikami
  initial_output_size = os.path.getsize(OUTPUT_FILE) if os.path.exists(OUTPUT_FILE) else 0

  addrs = read_addresses()

  while True:
    process = None
    try:
      # Przed każdą paczką: jeśli ktoś już zabrał nagrodę, nie ma czego szukać
      if not target_still_funded(addrs):
        print("\n[!] Adres celu jest PUSTY - ktoś już znalazł klucz i zabrał środki. Kończę szukanie.")
        show_alert_box("Adres celu jest pusty - nagroda została już zabrana. Szukanie zatrzymane.",
                       "Puzzle już rozwiązane")
        save_db(checked_starts)
        break

      start_val = get_random_start(checked_starts)
      start_hex = f"{start_val:018X}"
      end_val = start_val + CHUNK_SIZE - 1
      end_hex = f"{end_val:018X}"

      print("\n" + "=" * 60)
      print(f"[+] Startuję nową paczkę -> Od: {start_hex} Do: {end_hex}")
      print("=" * 60)

      cmd = [
          EXE_PATH,
          "-i",
          INPUT_FILE,
          "-o",
          OUTPUT_FILE,
          "-start",
          start_hex,
          "-range",
          str(RANGE_BITS),
          "-m",
          "1048576",
      ]
      print(f"[CMD] {' '.join(cmd)}")

      # Uruchomienie programu bezpośrednio (bez potoku i buforowania)
      t0 = time.time()
      process = subprocess.Popen(cmd)
      ret = process.wait()
      elapsed = time.time() - t0

      # Po zakończeniu paczki sprawdzamy czy plik znalezione.txt powiększył swój rozmiar
      current_output_size = os.path.getsize(OUTPUT_FILE) if os.path.exists(OUTPUT_FILE) else 0
      found_key = current_output_size > initial_output_size

      if found_key:
        print("\n[!] *** ZNALEZIONO KLUCZ! SPRAWDŹ PLIK ZNALEZIONE.TXT ***\n")
        show_alert_box("Znalazłem! Klucz został odnaleziony i zapisany w pliku 'znalezione.txt'.")
        initial_output_size = current_output_size

      # Zakres zapisujemy tylko, jeśli VanitySearch zakończył się poprawnie
      if ret != 0:
        print(f"[!] VanitySearch zakończył się kodem {ret} po {elapsed:.0f} s. "
              f"Zakres {start_hex} NIE zostaje zapisany jako sprawdzony.")
        time.sleep(10)
        continue

      checked_starts.add((start_val, RANGE_BITS))
      save_db(checked_starts)
      print(f"[✓] Zakres {start_hex} ukończony w {elapsed / 60:.1f} min i zapisany w bazie "
            f"(razem: {len(checked_starts)}).")

      time.sleep(1)

    except KeyboardInterrupt:
      print("\n[!] Przerwano przez użytkownika. Bieżący zakres NIE jest zapisany jako sprawdzony.")
      if process is not None and process.poll() is None:
        process.terminate()
        try:
          process.wait(timeout=10)
        except subprocess.TimeoutExpired:
          process.kill()
      save_db(checked_starts)
      print("[!] Baza zapisana. Możesz bezpiecznie zamknąć okno.")
      break
    except Exception as e:
      print(f"\n[!] Wystąpił błąd: {e}")
      break


if __name__ == "__main__":
  main()
