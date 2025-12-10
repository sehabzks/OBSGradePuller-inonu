import requests
from bs4 import BeautifulSoup
import re
import os
import shutil
from typing import List, Callable, Dict, Optional
from src.models import CourseGrade, ExamStats

class OBSClient:
    # --- URL SABİTLERİ ---
    BASE_URL = "https://obs.inonu.edu.tr/oibs/std/"
    LOGIN_URL = "https://obs.inonu.edu.tr/oibs/std/login.aspx"
    GRADES_URL = "https://obs.inonu.edu.tr/oibs/std/not_listesi_op.aspx"
    STATS_BASE_URL = "https://obs.inonu.edu.tr" # İstatistikler genelde /oibs/acd/ altında çıkıyor

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": self.LOGIN_URL,
            "Origin": "https://obs.inonu.edu.tr",
            "Cache-Control": "no-cache"
        })

    def _get_hidden_inputs(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Sayfadaki gizli inputları toplar (__VIEWSTATE vb.)."""
        data = {}
        for inp in soup.find_all("input", type="hidden"):
            if inp.get("name"):
                data[inp.get("name")] = inp.get("value", "")
        return data

    def _download_captcha(self, soup: BeautifulSoup) -> Optional[str]:
        """Captcha resmini indirir ve dosya yolunu döner."""
        img_tag = soup.find(id="imgCaptchaImg")
        if not img_tag: return None
        
        src = img_tag.get("src")
        # URL'yi düzelt
        if not src.startswith("http"):
            url = self.BASE_URL + src.lstrip("/") if src.startswith("/") else self.BASE_URL + src
        else:
            url = src

        r = self.session.get(url, stream=True)
        if r.status_code == 200:
            path = "temp_captcha.png"
            with open(path, "wb") as f:
                r.raw.decode_content = True
                shutil.copyfileobj(r.raw, f)
            return path
        return None

    def login(self, username: str, password: str, captcha_callback: Callable[[str], str]) -> bool:
        """
        Giriş işlemini yönetir.
        captcha_callback: Resmi gösterip kullanıcıdan kodu alan fonksiyondur.
        """
        # 1. Sayfayı Yükle
        r_get = self.session.get(self.LOGIN_URL)
        soup = BeautifulSoup(r_get.content, "html.parser")
        
        # 2. Captcha İndir ve Kullanıcıya Sor (Callback ile)
        captcha_path = self._download_captcha(soup)
        captcha_code = ""
        if captcha_path:
            # UI katmanına "Resim burada, bana kodu ver" diyoruz
            captcha_code = captcha_callback(captcha_path) 
        
        # 3. Payload Hazırla
        payload = self._get_hidden_inputs(soup)
        payload.update({
            "txtParamT01": username,
            "txtParamT02": password,
            "txtParamT1": password,
            "txtSecCode": captcha_code,
            "__EVENTTARGET": "btnLogin",
            "__EVENTARGUMENT": "",
            "txt_scrWidth": "1920", 
            "txt_scrHeight": "1080"
        })
        if "btnLogin" in payload: del payload["btnLogin"]

        # 4. Giriş Yap
        r_post = self.session.post(self.LOGIN_URL, data=payload)
        
        # Dosyayı temizle
        if captcha_path and os.path.exists(captcha_path):
            os.remove(captcha_path)

        # Başarılı mı?
        return "login.aspx" not in r_post.url

    def fetch_grades(self) -> List[CourseGrade]:
        """Tüm notları ve istatistikleri çeker."""
        self.session.headers.update({"Referer": self.GRADES_URL})
        r = self.session.get(self.GRADES_URL)
        soup = BeautifulSoup(r.content, "html.parser")
        
        table = soup.find(id="grd_not_listesi")
        if not table:
            raise Exception("Not tablosu bulunamadı! URL veya oturum hatalı olabilir.")
            
        # Dönem Bilgisi
        donem_val = "20251" # Default
        donem_select = soup.find("select", id="cmbDonemler")
        if donem_select:
            opt = donem_select.find("option", selected=True)
            if opt: donem_val = opt.get("value")

        grades_list = []
        rows = table.find_all("tr")[1:]

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 5: continue

            # Temel Bilgiler
            course_code = cols[1].get_text(strip=True)
            course_name = cols[2].get_text(strip=True)
            letter_grade = cols[6].get_text(strip=True)
            raw_text = cols[4].get_text(" ", strip=True)
            
            # Senin notlarını parse et
            my_grades = self._parse_my_grades(raw_text)
            
            # Sınıf Ortalamalarını Çek (AJAX İşlemleri)
            class_avgs = {"Vize": "?", "Final": "?", "Büt": "?"}
            
            stats_btn = row.find("a", id=re.compile(r"btnIstatistik"))
            if stats_btn:
                href = stats_btn.get("href", "")
                match = re.search(r"__doPostBack\('([^']*)'", href)
                if match:
                    target = match.group(1)
                    # Ortalamaları getir
                    class_avgs = self._fetch_course_stats(target, donem_val, soup)

            # Veriyi Modele Dök
            course = CourseGrade(
                code=course_code,
                name=course_name,
                term_id=donem_val,
                letter_grade=letter_grade,
                midterm=ExamStats(my_grades["Vize"], class_avgs["Vize"]),
                final=ExamStats(my_grades["Final"], class_avgs["Final"]),
                makeup=ExamStats(my_grades["Büt"], class_avgs["Büt"])
            )
            grades_list.append(course)

        return grades_list

    def _fetch_course_stats(self, target: str, donem: str, main_soup: BeautifulSoup) -> Dict[str, str]:
        """AJAX ile istatistik URL'sini bulur ve ortalamaları parse eder."""
        try:
            # 1. AJAX Trigger
            hidden_data = self._get_hidden_inputs(main_soup)
            hidden_data.update({
                "ScriptManager1": f"UpdatePanel1|{target}",
                "__EVENTTARGET": target,
                "__EVENTARGUMENT": "",
                "__ASYNCPOST": "true",
                "cmbDonemler": donem
            })
            
            self.session.headers.update({"X-MicrosoftAjax": "Delta=true"})
            r_post = self.session.post(self.GRADES_URL, data=hidden_data)
            
            # Header temizliği
            if "X-MicrosoftAjax" in self.session.headers: 
                del self.session.headers["X-MicrosoftAjax"]

            # 2. URL Bulma
            url_match = re.search(r"(Ders_Istatistik\.aspx[^'\"]*)", r_post.text)
            if not url_match:
                url_match = re.search(r"prolizPopup\('([^']+)'", r_post.text)
            
            if url_match:
                raw_url = url_match.group(1)
                full_url = ""
                if raw_url.startswith("http"): full_url = raw_url
                elif raw_url.startswith("/"): full_url = "https://obs.inonu.edu.tr" + raw_url
                else: full_url = "https://obs.inonu.edu.tr/oibs/std/" + raw_url.lstrip("/") # Fallback

                # 3. İstatistik Sayfasını İndir
                r_stats = self.session.get(full_url)
                return self._parse_averages_from_html(r_stats.text)
            
            return {"Vize": "?", "Final": "?", "Büt": "?"}

        except Exception:
            return {"Vize": "?", "Final": "?", "Büt": "?"}

    def _parse_my_grades(self, text: str) -> Dict[str, str]:
        """ 'Vize : 80 Final : --' stringini parse eder."""
        grades = {"Vize": "-", "Final": "-", "Büt": "-"}
        vize = re.search(r"Vize\s*:\s*([\d\w-]+)", text)
        final = re.search(r"Final\s*:\s*([\d\w-]+)", text)
        but = re.search(r"Bütünleme\s*:\s*([\d\w-]+)", text)
        
        if vize: grades["Vize"] = vize.group(1)
        if final: grades["Final"] = final.group(1)
        if but: grades["Büt"] = but.group(1)
        return grades

    def _parse_averages_from_html(self, html: str) -> Dict[str, str]:
        """State Machine mantığıyla tüm ortalamaları çeker."""
        averages = {"Vize": "?", "Final": "?", "Büt": "?"}
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="grdIstSnv")
        if not table: return averages

        context = None
        for row in table.find_all("tr"):
            text = row.get_text(strip=True)
            
            if "Ara Sınav" in text: context = "Vize"
            elif "Yarıyıl Sonu" in text or "Final" in text: context = "Final"
            elif "Bütünleme" in text: context = "Büt"
            
            if "not ortalaması" in text and context:
                cols = row.find_all("td")
                if len(cols) > 1:
                    averages[context] = cols[1].get_text(strip=True)
        
        return averages