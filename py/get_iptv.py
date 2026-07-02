import re
import os
import requests
import logging
import shutil
import threading
import chardet
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# === Configure Logger ===
def setupLogger():
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(formatter)
    logger.addHandler(consoleHandler)
    
    fileHandler = logging.FileHandler("logs/iptv_update.log", encoding="utf-8")
    fileHandler.setFormatter(formatter)
    logger.addHandler(fileHandler)
    return logger

logger = setupLogger()

writeLock = threading.Lock()

def ensureDir(filePath):
    """Ensure the directory of the file exists"""
    dirName = os.path.dirname(filePath)
    if dirName:
        os.makedirs(dirName, exist_ok=True)

def getSession():
    """Create a robust session with retry mechanisms and standard browser headers"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive"
    })
    retry = Retry(connect=3, read=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def loadUrlsFromFile(filePath):
    """Load URL list from a text file"""
    urls = []
    if not os.path.exists(filePath):
        logger.warning(f"URL configuration file not found: {filePath}")
        return urls
    try:
        with open(filePath, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        logger.info(f"Loaded {len(urls)} sources from {filePath}")
    except Exception as e:
        logger.error(f"Failed to read URL file: {e}")
    return urls

def parseTemplate(templateFile):
    """Parse template file structure"""
    templateChannels = OrderedDict()
    currentCategory = None
    try:
        with open(templateFile, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "#genre#" in line:
                    currentCategory = line.split(",")[0].strip()
                    templateChannels[currentCategory] = []
                elif currentCategory:
                    channelName = line.split(",")[0].strip()
                    if channelName:
                        templateChannels[currentCategory].append(channelName)
    except FileNotFoundError:
        logger.warning(f"Template file not found: {templateFile}")
        return None 
    return templateChannels

def fetchChannels(url):
    """Fetch and decode channels from remote URL safely handling gzip and encodings"""
    channels = OrderedDict()
    with getSession() as session:
        try:
            # Use content instead of raw stream to automatically decode content-encoding like gzip
            response = session.get(url, timeout=15)
            response.raise_for_status()
            rawContent = response.content
            if not rawContent:
                return channels
            
            # Detect encoding using the first 50KB chunk securely
            sampleChunk = rawContent[:51200]
            detected = chardet.detect(sampleChunk)
            encoding = detected['encoding'] if detected and detected['encoding'] else 'utf-8'
            
            fullText = rawContent.decode(encoding, errors='ignore')
                
        except Exception as e:
            logger.error(f"Network fetch exception for {url}: {e}")
            return channels

    lines = [line.strip() for line in fullText.splitlines() if line.strip()]
    if not lines:
        return channels

    isM3u = any("#EXTINF" in line for line in lines[:10])

    if isM3u:
        currentCategory = "默认分类"
        currentName = "未知频道"
        reGroup = re.compile(r'group-title=["\']([^"\']*)["\']')
        reName = re.compile(r',([^,]*)$')

        for line in lines:
            if line.startswith("#EXTINF"):
                groupMatch = reGroup.search(line)
                if groupMatch:
                    currentCategory = groupMatch.group(1).strip()
                nameMatch = reName.search(line)
                if nameMatch:
                    currentName = nameMatch.group(1).strip()
            elif not line.startswith("#") and "://" in line:
                if currentCategory not in channels:
                    channels[currentCategory] = []
                if currentName and currentName != "未知频道":
                    channels[currentCategory].append((currentName, line.strip()))
                currentName = "未知频道"
    else:
        currentCategory = None
        for line in lines:
            if "#genre#" in line:
                currentCategory = line.split(",")[0].strip()
                if currentCategory not in channels:
                    channels[currentCategory] = []
            elif currentCategory and "," in line:
                parts = line.split(",", 1)
                if len(parts) == 2:
                    name, urlPart = parts
                    if name.strip() and urlPart.strip():
                        channels[currentCategory].append((name.strip(), urlPart.strip()))
    return channels

def matchChannels(templateChannels, allChannels):
    """Match source channels with template using enhanced regex bounds and hash mapping"""
    matched = OrderedDict()
    unmatchedTemplate = OrderedDict()

    # 1. Establish global inverted index map
    sourceMap = {}
    for cat, chans in allChannels.items():
        for name, url in chans:
            normName = name.lower().strip()
            if normName not in sourceMap:
                sourceMap[normName] = []
            sourceMap[normName].append({'name': name, 'url': url, 'cat': cat})

    usedChannelKeys = set()

    for cat in templateChannels:
        matched[cat] = OrderedDict()
        unmatchedTemplate[cat] = []

    # 2. Match with enhanced boundaries (blocking unwanted alphanumeric and Chinese prefixes/suffixes)
    for category, tmplNames in templateChannels.items():
        for tmplName in tmplNames:
            
            variantsRaw = [n.strip() for n in tmplName.split("|") if n.strip()]
            variants = list(OrderedDict.fromkeys(variantsRaw))

            primaryName = variants[0]
            foundForThisTemplate = False

            for variant in variants:
                variantLower = variant.lower()
                
                # Enhanced strict boundary assertion: blocks english letters, digits, and Chinese characters
                # Prevents "CCTV1" matching "CCTV11" or "广东CCTV1"
                pattern = re.compile(
                    r'(?<![a-zA-Z0-9\u4e00-\u9fa5])' + 
                    re.escape(variantLower) + 
                    r'(?![a-zA-Z0-9\+\u4e00-\u9fa5])'
                )

                targetKeys = [k for k in sourceMap.keys() if variantLower in k]

                for srcNameLower in targetKeys:
                    if pattern.search(srcNameLower):
                        for src in sourceMap[srcNameLower]:
                            key = f"{src['name']}_{src['url']}"
                            if key in usedChannelKeys:
                                continue

                            if primaryName not in matched[category]:
                                matched[category][primaryName] = []

                            matched[category][primaryName].append((src['name'], src['url']))
                            usedChannelKeys.add(key)
                            foundForThisTemplate = True

            if not foundForThisTemplate:
                unmatchedTemplate[category].append(tmplName)

    # 3. Filter unmatched source channels
    unmatchedSource = OrderedDict()
    for srcNameLower, srcList in sourceMap.items():
        for src in srcList:
            key = f"{src['name']}_{src['url']}"
            if key not in usedChannelKeys:
                if src['cat'] not in unmatchedSource:
                    unmatchedSource[src['cat']] = []
                unmatchedSource[src['cat']].append((src['name'], src['url']))

    return matched, unmatchedTemplate, unmatchedSource

def isIpv6(url):
    return "://[" in url

def generateOutputs(channels, templateChannels, m3uPath, txtPath):
    """Generate final M3U and TXT output files cleanly"""
    writtenUrls = set()
    ensureDir(m3uPath)
    ensureDir(txtPath)

    try:
        with writeLock:
            with open(m3uPath, "w", encoding="utf-8") as m3u, \
                 open(txtPath, "w", encoding="utf-8") as txt:

                m3u.write("#EXTM3U\n")

                for category in templateChannels:
                    if category not in channels or not channels[category]:
                        continue

                    txt.write(f"\n{category},#genre#\n")

                    for channelKeyName, channelList in channels[category].items():
                        uniqueUrls = []
                        seenUrls = set()

                        for _, url in channelList:
                            if url not in seenUrls and url not in writtenUrls:
                                uniqueUrls.append(url)
                                seenUrls.add(url)
                                writtenUrls.add(url)

                        totalLines = len(uniqueUrls)
                        for idx, url in enumerate(uniqueUrls, 1):
                            baseUrl = re.split(r'[$#]', url)[0].strip()
                            suffixName = "IPV6" if isIpv6(url) else "IPV4"

                            displayName = channelKeyName
                            metaSuffix = f"$LR•{suffixName}"
                            if totalLines > 1:
                                metaSuffix += f"•{totalLines}『线路{idx}』"

                            finalUrl = f"{baseUrl}{metaSuffix}"
                            safeDisplayName = displayName.replace('"', '\\"')
                            m3u.write(f'#EXTINF:-1 tvg-name="{safeDisplayName}" group-title="{category}",{displayName}\n')
                            m3u.write(f"{finalUrl}\n")

                            txt.write(f"{displayName},{finalUrl}\n")

        logger.info(f"Outputs generated: {m3uPath}, {txtPath}")
    except Exception as e:
        logger.error(f"Failed to write output files: {e}")

def generateUnmatchedReport(unmatchedTemplate, unmatchedSource, reportFile):
    """Generate reports for unmatched elements"""
    totalTemplateLost = sum(len(v) for v in unmatchedTemplate.values())
    
    if not reportFile:
        return totalTemplateLost

    ensureDir(reportFile)
    try:
        with open(reportFile, "w", encoding="utf-8") as f:
            f.write(f"# Unmatched Report {datetime.now()}\n")
            f.write(f"# Lost templates count: {totalTemplateLost}\n\n")
            f.write("## In Template but missing in Source\n")
            for cat, names in unmatchedTemplate.items():
                if names:
                    f.write(f"\n{cat},#genre#\n")
                    for name in list(OrderedDict.fromkeys(names)):
                        f.write(f"{name},\n")

            f.write("\n\n## In Source but missing in Template\n")
            for cat, chans in unmatchedSource.items():
                if chans:
                    f.write(f"\n{cat},#genre#\n")
                    uniqueNames = list(OrderedDict.fromkeys([c[0] for c in chans]))
                    for name in uniqueNames:
                        f.write(f"{name},\n")
        logger.info(f"Report generated: {reportFile}")
        return totalTemplateLost
    except Exception as e:
        logger.error(f"Failed to generate report: {e}")
        return 0

def removeUnmatchedFromTemplate(templateFile, unmatchedTemplate):
    """Clean up invalid channels from template file"""
    backupFile = templateFile + ".backup"
    try:
        shutil.copy2(templateFile, backupFile)
        with open(templateFile, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        newLines = []
        currentCat = None
        toRemove = {cat: set(names) for cat, names in unmatchedTemplate.items()}

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                newLines.append(line)
                continue
            if "#genre#" in stripped:
                currentCat = stripped.split(",")[0].strip()
                newLines.append(line)
                continue
            if currentCat:
                name = stripped.split(",")[0].strip()
                if currentCat in toRemove and name in toRemove[currentCat]:
                    continue
                newLines.append(line)
            else:
                newLines.append(line)

        with open(templateFile, "w", encoding="utf-8") as f:
            f.writelines(newLines)
        logger.info(f"Removed invalid channels from template {templateFile}")
    except Exception as e:
        logger.error(f"Failed to update template: {e}")

def processIptvTask(templateFile, tvUrls, outputM3u, outputTxt, reportFile, autoClean=True):
    logger.info(f"=== Starting task: {templateFile} ===")
    template = parseTemplate(templateFile)
    if not template:
        return

    logger.info(f"Fetching data from {len(tvUrls)} sources...")
    allChannels = OrderedDict()
    successCount = 0
    failCount = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        futureToUrl = {executor.submit(fetchChannels, url): url for url in tvUrls}
        for future in as_completed(futureToUrl):
            url = futureToUrl[future]
            try:
                data = future.result()
                if data:
                    successCount += 1
                    for cat, chans in data.items():
                        if cat not in allChannels:
                            allChannels[cat] = []
                        allChannels[cat].extend(chans)
                else:
                    failCount += 1
            except Exception as e:
                failCount += 1
                logger.error(f"Thread execution anomaly for source {url}: {e}")

    logger.info(f"Fetch completed: {successCount} success, {failCount} failed.")
    logger.info("Matching channels...")
    
    matched, unmatchedTmpl, unmatchedSrc = matchChannels(template, allChannels)
    
    generateOutputs(matched, template, outputM3u, outputTxt)
    lostCount = generateUnmatchedReport(unmatchedTmpl, unmatchedSrc, reportFile)

    if autoClean and lostCount > 0:
        logger.info(f"Cleaning {lostCount} invalid channels...")
        removeUnmatchedFromTemplate(templateFile, unmatchedTmpl)
        
    logger.info(f"=== Task completed: {templateFile} ===\n")

if __name__ == "__main__":
    urlsFile = "py/config/urls.txt"
    
    tvUrls = loadUrlsFromFile(urlsFile)
    if not tvUrls:
        logger.warning("No URLs loaded from file, using empty list")
        tvUrls = [] 

    processIptvTask(
        templateFile="py/config/iptv.txt",
        tvUrls=tvUrls,
        outputM3u="lib/iptv.m3u",
        outputTxt="lib/iptv.txt",
        reportFile="py/config/iptv.log",
        autoClean=False
    )

    testTemplateFile = "py/config/iptv_test.txt"
    if os.path.exists(testTemplateFile):
        processIptvTask(
            templateFile=testTemplateFile,
            tvUrls=tvUrls,
            outputM3u="lib/iptv_test.m3u",
            outputTxt="lib/iptv_test.txt",
            reportFile=None,
            autoClean=False 
        )
    else:
        logger.info(f"Test config {testTemplateFile} not found, skipping.")
