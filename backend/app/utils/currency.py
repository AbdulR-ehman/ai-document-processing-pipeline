"""ISO 4217 currency codes (active set).

Kept as a plain frozenset so business validation can check currency codes
without any dependency or network access.
"""

from __future__ import annotations

#: Active ISO 4217 alphabetic codes.
ISO_4217_CODES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
    BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC
    CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF
    GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR
    KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT
    MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK
    PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS
    SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN
    UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XDR XOF
    XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWL
    """.split()
)

#: Codes that are technically valid ISO 4217 but not real currencies.
NON_CURRENCY_CODES: frozenset[str] = frozenset(
    {"XXX", "XTS", "XSU", "XUA", "USN", "CHE", "CHW", "COU", "MXV", "UYI", "UYW", "CLF"}
)


def is_valid_currency(code: str | None) -> bool:
    """True when ``code`` is an active, real ISO 4217 currency code."""
    if not code or not isinstance(code, str):
        return False
    upper = code.strip().upper()
    return upper in ISO_4217_CODES and upper not in NON_CURRENCY_CODES
