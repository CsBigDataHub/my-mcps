#!/usr/bin/env bb
;; MCP server for elisp evaluation via emacsclient.
;; Writes code to a temp file and evals it - no shell escaping issues.
;;
;; Author: Ag Ibragimov - github.com/agzam
;; Based on Ovi Stoica's suggestion on Clojurians: https://clojurians.slack.com/archives/C099W16KZ/p1770835002976539?thread_ts=1770820230.642669&cid=C099W16KZ

(require '[cheshire.core :as json]
         '[clojure.java.shell :as shell]
         '[clojure.string :as str])

(import '[java.io File])

(def server-info
  {:name "elisp-eval" :version "1.0.0"})

(def tool-def
  {:name "elisp-eval"
   :description "Evaluate Emacs Lisp code in the running Emacs server. Returns the result of evaluation along with any new *Messages* output produced during evaluation. State persists between calls. Only the return value of the last expression is captured. Write elisp naturally with no escaping needed. You can also read special buffers like *Messages*, *compilation*, etc. via (with-current-buffer BUF (buffer-string))."
   :inputSchema
   {:type "object"
    :properties {:code {:type "string"
                        :description "Emacs Lisp code to evaluate."}}
    :required ["code"]}})

(defn eval-elisp [code]
  (let [tmp      (File/createTempFile "eca-elisp-" ".el")
        msgs-tmp (File/createTempFile "eca-msgs-" ".txt")
        path     (.getAbsolutePath tmp)
        msgs-path (.getAbsolutePath msgs-tmp)]
    (try
      (spit tmp code)
      (let [wrapper (format "(let* ((msgs-buf (get-buffer-create \"*Messages*\"))
       (msgs-pos (with-current-buffer msgs-buf (point-max)))
       (result (with-temp-buffer
                 (insert-file-contents \"%s\")
                 (goto-char (point-min))
                 (let (forms)
                   (condition-case nil
                       (while t (push (read (current-buffer)) forms))
                     (end-of-file nil))
                   (eval (cons 'progn (nreverse forms)) t))))
       (new-msgs (with-current-buffer msgs-buf
                   (let ((s (string-trim (buffer-substring-no-properties msgs-pos (point-max)))))
                     (and (not (string-empty-p s)) s)))))
  (when new-msgs
    (write-region new-msgs nil \"%s\" nil 'silent))
  result)" path msgs-path)
            {:keys [exit out err]} (shell/sh "emacsclient" "--eval" wrapper)
            messages (let [s (str/trim (slurp msgs-tmp))]
                       (when-not (str/blank? s) s))]
        (if (zero? exit)
          {:content (cond-> [{:type "text" :text (str/trim out)}]
                      messages (conj {:type "text" :text (str "--- *Messages* ---\n" messages)}))}
          {:content [{:type "text" :text (str/trim (str out err))}] :isError true}))
      (finally
        (.delete tmp)
        (.delete msgs-tmp)))))

(defn handle-request [{:strs [id method params]}]
  (case method
    "initialize"
    {:jsonrpc "2.0" :id id
     :result {:protocolVersion "2024-11-05"
              :capabilities {:tools {}}
              :serverInfo server-info}}

    "notifications/initialized" nil

    "tools/list"
    {:jsonrpc "2.0" :id id
     :result {:tools [tool-def]}}

    "tools/call"
    (let [{tool "name" args "arguments"} params
          code (get args "code")]
      {:jsonrpc "2.0" :id id
       :result (if (= tool "elisp-eval")
                 (eval-elisp code)
                 {:content [{:type "text" :text (str "Unknown tool: " tool)}]
                  :isError true})})

    ;; Unknown method - ignore
    nil))

(doseq [line (line-seq (java.io.BufferedReader. *in*))]
  (when-not (str/blank? line)
    (when-let [res (handle-request (json/parse-string line))]
      (println (json/generate-string res))
      (flush))))
