            cmd = build_ytdlp_command(
                output_template, start, end, file_type, quality, clean_url, client_str, use_cookie
            )

            result = subprocess.run(
                cmd,
                cwd=str(job_dir),
                capture_output=True,
                text=True,
                timeout=MAX_PROCESS_SECONDS,
                check=False,
                shell=False,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "TMPDIR": str(job_dir),
                    "PYTHONUNBUFFERED": "1",
                },
            )

            if result.returncode == 0:
                success = True
                break
            else:
                last_stderr = (result.stderr or "")[-1500:]
                app.logger.warning("Strategy [%s] failed: %s", client_str, last_stderr)

        if cookie_file and cookie_file.exists():
            cookie_file.unlink(missing_ok=True)

        if not success:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None

            if "Sign in to confirm" in last_stderr or "bot" in last_stderr.lower():
                return jsonify({
                    "error": "YouTube बॉट-जाँच (Bot Check) सक्रिय है। कृपया Render के 'Secret Files' में cookies.txt जोड़ें।"
                }), 422

            if "Requested format is not available" in last_stderr or "No video formats found" in last_stderr:
                return jsonify({
                    "error": "YouTube ने इस समय वीडियो के डाउनलोड योग्य formats उपलब्ध नहीं कराए। PO Token और player-client के साथ दोबारा प्रयास किया गया, लेकिन स्ट्रीम नहीं मिली। कुछ देर बाद फिर प्रयास करें।"
                }), 422

            return jsonify({
                "error": "YouTube से वीडियो स्ट्रीम प्राप्त नहीं हो सकी। यह YouTube की अस्थायी रोक, account/cookie जाँच या वीडियो की उपलब्धता के कारण हो सकता है। कृपया कुछ देर बाद पुनः प्रयास करें।"
            }), 422

        candidates = [
            p for p in job_dir.iterdir()
            if p.is_file()
            and not p.is_symlink()
            and p.suffix.lower() in {".mp4", ".mp3", ".webm", ".m4a"}
        ]

        if not candidates:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None
            return jsonify({"error": "क्लिप तैयार नहीं हो सकी। कृपया समय की जाँच करें।"}), 500

        output_file = max(candidates, key=lambda p: p.stat().st_size)
        file_size = output_file.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None
            return jsonify({"error": "तैयार क्लिप 500 MB से बड़ी है। कृपया कम क्वालिटी चुनें।"}), 413

        return jsonify({
            "message": "आपकी क्लिप सफलतापूर्वक तैयार हो गई है!",
            "format": output_file.suffix.lower().lstrip("."),
            "size_mb": round(file_size / (1024 * 1024), 2),
            "job_id": job_id,
            "filename": output_file.name,
            "download_url": f"/download/{job_id}/{output_file.name}"
        })

    except subprocess.TimeoutExpired:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "प्रोसेसिंग का समय समाप्त हो गया। कृपया छोटी क्लिप या 720p क्वालिटी चुनें।"}), 504

    except Exception:
        app.logger.error("Unexpected error during processing.")
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "सर्वर में आंतरिक समस्या आई। कृपया पुनः प्रयास करें।"}), 500

    finally:
        job_lock.release()


@app.get("/download/<job_id>/<filename>")
def download_file(job_id, filename):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"error": "अमान्य डाउनलोड लिंक।"}), 400

    if not re.fullmatch(r"clip\.(mp4|mp3|webm|m4a)", filename):
        return jsonify({"error": "अमान्य फाइल नाम।"}), 400

    job_dir = CLIP_DIR / job_id
    path = job_dir / filename

    if not path.is_file() or path.is_symlink():
        return jsonify({"error": "फाइल नहीं मिली या समय समाप्त होने के कारण हटा दी गई है।"}), 404

    if time.time() - path.stat().st_mtime >= FILE_TTL_SECONDS:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "डाउनलोड लिंक की 30 मिनट की वैधता समाप्त हो गई है।"}), 410

    return send_from_directory(
        str(job_dir),
        filename,
        as_attachment=True,
        download_name=f"rkstudio_{job_id[:6]}_{filename}",
        conditional=True,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        debug=False,
    )
