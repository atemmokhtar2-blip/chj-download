def get_main_keyboard(lang: str, user_id: int = None) -> ReplyKeyboardMarkup:
    if lang == "ar":
        buttons = [
            [t("ar", "menu_download")],
            [t("ar", "menu_support")],
        ]
        if user_id and is_admin(user_id):
            buttons.append([t("ar", "menu_admin")])
    else:
        buttons = [
            [t("en", "menu_download")],
            [t("en", "menu_support")],
        ]
        if user_id and is_admin(user_id):
            buttons.append([t("en", "menu_admin")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)
