import os

files_to_check = [
    r"generator\templates\generator\dashboard.html",
    r"generator\templates\generator\history.html",
    r"generator\templates\generator\pricing.html",
    r"users\templates\users\register.html",
    r"users\templates\users\login.html",
    r"templates\terms.html",
    r"templates\privacy.html"
]

for file_path in files_to_check:
    if not os.path.exists(file_path):
        continue
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    new_content = content.replace("cover letter", "resume")
    new_content = new_content.replace("Cover Letter", "Resume")
    new_content = new_content.replace("cover letters", "resumes")
    new_content = new_content.replace("Cover Letters", "Resumes")
    
    if new_content != content:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Updated {file_path}")
