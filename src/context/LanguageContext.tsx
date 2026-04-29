import { createContext, useContext, useState, type ReactNode } from "react";
import { i18n, type Lang, type Translations } from "../i18n";

interface LangContextValue {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: (key: keyof Translations) => string;
}

export const LanguageContext = createContext<LangContextValue>({
  lang: "en",
  setLang: () => {},
  t: (key) => i18n.en[key],
});

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>("en");
  const t = (key: keyof Translations): string => i18n[lang][key];
  return (
    <LanguageContext.Provider value={{ lang, setLang, t }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLang(): LangContextValue {
  return useContext(LanguageContext);
}
