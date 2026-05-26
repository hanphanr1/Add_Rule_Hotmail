# Hotmail Rule Creator - Old Method

Bản cũ dùng Outlook Lite OAuth flow giống `newmethod_netflix.py`.

## Files

- `rule_creator.py` - CLI test nhanh
- `rule_creator_gui.py` - GUI app PyQt5
- `build.bat` - build ra `.exe`
- `requirements.txt` - dependencies

## Chạy GUI

```powershell
cd C:\Users\DELL\Documents\project_Add_rule_hotmail\new_method
python -m pip install -r requirements.txt
python rule_creator_gui.py
```

## Chạy CLI

```powershell
python rule_creator.py email@hotmail.com:password
python rule_creator.py email@hotmail.com|password
```

## Build exe

```powershell
build.bat
```

Output:

```text
dist\RuleCreator.exe
```

## App hỗ trợ

- Paste nhiều account dạng `email:password`
- Paste nhiều account dạng `email|password`
- Chọn rule Netflix cần tạo
- Chọn API method:
  - Outlook REST v2.0
  - Microsoft Graph
- Multi-thread
- Export results

## Rules

Forward về:

```text
netflix-{localpart}@tm.cameyou.shop
```

Ví dụ:

```text
abc@hotmail.com -> netflix-abc@tm.cameyou.shop
```

Rules:

- Netflix - Login Code
- Netflix - Family/Temp Access
- Netflix - All from sender

## Lưu ý

Method này chỉ lấy được `authorization code` nếu account không bị Microsoft checkpoint/protect/verify.
#
