from pathlib import Path
p=Path('bot/handlers/onboarding.py')
t=p.read_text(encoding='utf-8')
old='''    text = (
        "Выбери тариф:\n\n"
        "Месяц: 750 руб / 30 дней\n"
        "Год: 2500 руб / 365 дней"
    )'''
new='''    text = (
        "Выбери тариф:\n\n"
        "Месячная подписка — 750 руб/месяц\n"
        "• Ежемесячная оплата\n\n"
        "Годовая подписка — 2500 руб/в год ( или всего 210 руб/мес.)\n"
        "• Экономия 6 500 руб/ в год\n"
        "• Оплата раз в год\n\n"
        "Подписку можно отменить в любой удобный момент в Личном кабинете бота"
    )'''
if old not in t:
    raise SystemExit('old block not found')
t=t.replace(old,new,1)
p.write_text(t,encoding='utf-8')
print('onboarding block replaced')
